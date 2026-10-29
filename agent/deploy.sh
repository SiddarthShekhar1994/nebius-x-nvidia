#!/usr/bin/env bash
# Copy Keeper's code, skills and persona into the sandbox. Run in WSL from the repo root:
#   bash agent/deploy.sh [sandbox]
# Safe to rerun after every change.
# NemoClaw puts nemohermes and openshell in ~/.local/bin, and nemohermes runs
# on nvm's Node. Load both so this also works from `bash script.sh` in PowerShell.
export PATH="$HOME/.local/bin:$PATH"
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1; fi
set -euo pipefail
for tool in nemohermes openshell; do
  command -v "$tool" >/dev/null || { echo "$tool not found. Run this from the Ubuntu (WSL) window where NemoClaw is installed." >&2; exit 1; }
done
SANDBOX="${1:-keeper}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
nh() { nemohermes "$SANDBOX" "$@"; }

# Stage everything on the Linux filesystem first. OpenShell's upload reads
# files under /mnt/c (the Windows drive) as all zero bytes, so nothing is
# uploaded straight from the repo checkout.
echo "== stage code, the keeper command and skills"
mkdir -p "$STAGE/app" "$STAGE/bin" "$STAGE/skills"
cp -r "$ROOT/router" "$ROOT/memory" "$ROOT/skills" "$ROOT/keeper_core" "$ROOT/integrations" "$ROOT/triage" "$ROOT/actions" "$STAGE/app/"
mkdir -p "$STAGE/app/demo/fixtures"
cp "$ROOT/demo/fixtures/inbox_75.json" "$STAGE/app/demo/fixtures/"
rm -rf "$STAGE/app/skills/hermes"
find "$STAGE/app" -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/agent/bin/keeper" "$STAGE/bin/keeper"
chmod +x "$STAGE/bin/keeper"
cp -r "$ROOT"/skills/hermes/*/ "$STAGE/skills/"

# Upload copies a folder *into* the destination, so each folder goes to its
# parent. Replace Keeper's code and shipped skills, but leave data/ and any
# skills you approved alone.
echo "== upload code and the keeper command"
nh exec -- rm -rf /sandbox/keeper/app /sandbox/keeper/bin
nh exec -- mkdir -p /sandbox/keeper/data /sandbox/.hermes/skills/keeper
nh upload "$STAGE/app" /sandbox/keeper/
nh upload "$STAGE/bin" /sandbox/keeper/
nh exec -- chmod +x /sandbox/keeper/bin/keeper

echo "== install Keeper's skills"
for dir in "$STAGE"/skills/*/; do
  name="$(basename "$dir")"
  nh exec -- rm -rf "/sandbox/.hermes/skills/keeper/$name"
  nh upload "${dir%/}" /sandbox/.hermes/skills/keeper/
  echo "   $name"
done

echo "== merge the Keeper section into the persona files"
# Hermes in a NemoClaw sandbox reads /sandbox/.hermes/runtime/SOUL.md, the
# folder NemoClaw moved state.db into (`keeper skills trace-prompt` shows the
# load_soul_md call). NemoClaw's docs point at /sandbox/SOUL.md and Hermes's at
# /sandbox/.hermes/SOUL.md, so Keeper keeps all three in sync. Learned rules
# and anything you added are kept; NemoClaw's stock "You are Hermes Agent"
# paragraph is dropped.
mkdir -p "$HOME/keeper-backups"
targets=(/sandbox /sandbox/.hermes)
if nh exec -- test -d /sandbox/.hermes/runtime >/dev/null 2>&1; then
  targets+=(/sandbox/.hermes/runtime)
fi
previous=""
for remote_dir in "${targets[@]}"; do
  work="$STAGE/soul$(echo "$remote_dir" | tr / _)"
  mkdir -p "$work"
  if nh download "$remote_dir/SOUL.md" "$work/" >/dev/null 2>&1 && [ -f "$work/SOUL.md" ]; then
    cp "$work/SOUL.md" "$HOME/keeper-backups/SOUL$(echo "$remote_dir" | tr / _).md.$(date +%Y%m%d-%H%M%S)"
  elif [ -n "$previous" ]; then
    # A file that doesn't exist yet starts from the one before it, so it gets
    # the learned rules too.
    cp "$previous" "$work/SOUL.md"
  else
    : > "$work/SOUL.md"
  fi
  # Keeper's section goes first (the model takes its identity from the top),
  # then learned rules and whatever else was there. See skills/soul.py.
  (cd "$ROOT" && PYTHONDONTWRITEBYTECODE=1 python3 -m skills.soul merge "$work/SOUL.md" agent/SOUL.md)
  nh upload "$work/SOUL.md" "$remote_dir/"
  previous="$work/SOUL.md"
  echo "   $remote_dir/SOUL.md"
done

echo "== Hermes settings"
# Every skill write waits for /skills approve <id>.
nh exec -- hermes config set skills.write_approval true
# Let terminal commands see the Token Factory placeholder. Hermes strips
# key-like variables by default; the real key never enters the sandbox.
nh exec -- hermes config set terminal.env_passthrough '["TOKENFACTORY_API_KEY"]'
# /teach, /keep, /drop and /apps: typed by the user, run by Hermes without the model.
# --force: on a rerun the section already exists, and Hermes refuses to
# replace a whole section without it.
nh exec -- hermes config set --force quick_commands '{"teach": {"type": "exec", "command": "/sandbox/keeper/bin/keeper skills teach"}, "keep": {"type": "exec", "command": "/sandbox/keeper/bin/keeper skills keep"}, "drop": {"type": "exec", "command": "/sandbox/keeper/bin/keeper skills drop"}, "apps": {"type": "exec", "command": "/sandbox/keeper/bin/keeper apps telegram"}}'

echo "== check the deployed files"
nh exec -- /sandbox/keeper/bin/keeper skills list

echo
echo "Deployed. Restart Hermes so it picks up SOUL.md and the setting:"
echo "  nemohermes $SANDBOX stop && nemohermes $SANDBOX start"
