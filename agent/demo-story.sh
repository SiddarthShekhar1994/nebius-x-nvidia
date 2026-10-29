#!/usr/bin/env bash
# The demo story: Keeper follows one tracked internship, Example Inc / Software Engineer Intern,
# from application to an extended deadline, and refuses to act on a fake recruiter. Run in
# Ubuntu/WSL from the repo root, after the 75-email inbox is loaded (demo/SEED_AND_RESET.md)
# and agent/openshell/setup-google.sh has run (docs/google-authorization.md):
#   bash agent/demo-story.sh track   # Keeper starts tracking the internship (found Oct 26)
#   bash agent/demo-story.sh part1   # application received, interview invitation, take-home due Nov 6
#   bash agent/demo-story.sh part2   # deadline extended to Nov 13, a fake recruiter asking for a fee
# Each part adds its emails to the demo inbox, sorts and labels them inside the sandbox, then acts:
# tracker, calendar, unsent drafts. Rerunning a step repeats nothing it already did.
export PATH="$HOME/.local/bin:$PATH"
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1; fi
set -euo pipefail

STEP="${1:-}"
SANDBOX="${2:-keeper}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${KEEPER_HOST_PYTHON:-$HOME/keeper-venv/bin/python}"     # host venv with requirements.txt
SEED_TOKEN="${KEEPER_SEED_TOKEN:-$HOME/keeper-secrets/seed.json}"
GOOGLE_TOKEN=/sandbox/keeper/secrets/google-token.json
nh() { nemohermes "$SANDBOX" "$@"; }
keeper() { nh exec -- /sandbox/keeper/bin/keeper "$@"; }

case "$STEP" in
  track)
    keeper apps add --organization "Example Inc" --title "Software Engineer Intern" \
      --url https://careers.example.test/jobs/software-engineer-intern --found-at 2026-10-26T12:00:00Z
    echo
    keeper apps telegram
    ;;
  part1|part2)
    [ -f data/demo-seed.json ] || { echo "data/demo-seed.json not found. Load the demo inbox first." >&2; exit 1; }
    ACCOUNT="$(python3 -c "import json; print(json.load(open('data/demo-seed.json'))['account'])")"
    MANIFEST="data/story-${STEP}-seed.json"
    NAME="$(basename "$MANIFEST")"
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    chmod 700 "$STAGE"

    echo "== 1/4 add the ${STEP} emails to the demo inbox"
    "$PY" -m demo.mailbox seed --token "$SEED_TOKEN" --account "$ACCOUNT" \
      --fixture "demo/fixtures/story_${STEP}.json" --manifest "$MANIFEST"

    echo "== 2/4 copy their manifest into the sandbox"
    # OpenShell uploads from /mnt/c can arrive as zero bytes, so stage a copy first.
    install -m 600 "$MANIFEST" "$STAGE/$NAME.new"
    nh upload "$STAGE/$NAME.new" /sandbox/keeper/data/
    nh exec -- mv -f "/sandbox/keeper/data/$NAME.new" "/sandbox/keeper/data/$NAME"

    echo "== 3/4 sort them with Nemotron and label them in Gmail"
    status=0
    keeper triage run --gmail --live --apply --account "$ACCOUNT" --token "$GOOGLE_TOKEN" \
      --manifest "/sandbox/keeper/data/$NAME" > "$STAGE/triage.json" || status=$?
    [ "$status" -ne 2 ] || { echo "Triage stopped; nothing was acted on." >&2; exit 2; }

    echo "== 4/4 act: tracker, calendar, unsent drafts"
    keeper act run --gmail --account "$ACCOUNT" --token "$GOOGLE_TOKEN" --apply > "$STAGE/act.json" || status=$?
    [ -s "$STAGE/act.json" ] || { echo "keeper act stopped; see the message above." >&2; exit 2; }

    echo
    python3 - "$MANIFEST" "demo/fixtures/story_${STEP}.json" "$STAGE/triage.json" "$STAGE/act.json" <<'EOF'
import json, sys
manifest, fixture, triage, act = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
gmail_id = {story_id: entry["provider_id"] for story_id, entry in manifest["messages"].items()}
rows = {row.get("fixture_id"): row for row in triage["results"]}
for email in fixture["emails"]:
    story_id = email["id"]
    print(f"{story_id}  {email['subject'].removeprefix('[SYNTHETIC] ')}")
    row = rows.get(story_id)
    if row:
        deadline = f", deadline {row['extracted_deadline'][:16]}Z" if row.get("extracted_deadline") else ""
        warning = "  SCAM WARNING: " + ", ".join(row["scam_signals"]) if row.get("scam_signals") else ""
        print(f"    triage    {row['bucket'] or 'review'}{deadline}{warning}")
    for outcome in act["outcomes"]:
        if outcome["email_id"] == "gmail:" + gmail_id.get(story_id, "?"):
            print(f"    {outcome['step']:<9} {outcome['status']}: {outcome['detail']}")
    print()
EOF
    keeper apps telegram
    exit "$status"
    ;;
  *)
    echo "usage: bash agent/demo-story.sh {track|part1|part2} [sandbox]" >&2
    exit 2
    ;;
esac
