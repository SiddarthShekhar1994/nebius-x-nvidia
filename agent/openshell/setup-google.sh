#!/usr/bin/env bash
# Everything the sandbox needs to work on the demo Gmail account: the runtime
# token, the seed manifest (which demo emails Keeper may touch), and the network
# policy that opens Gmail and Calendar to Keeper and keeps sending mail blocked.
# Run in Ubuntu/WSL from the repo root, after loading the demo inbox
# (demo/SEED_AND_RESET.md) and creating the token on Windows
# (docs/google-authorization.md):
#   bash agent/openshell/setup-google.sh /mnt/c/Users/YOU/keeper-secrets/google-token.json [sandbox]
# Safe to rerun, for example after re-creating the token.
export PATH="$HOME/.local/bin:$PATH"
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1; fi
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 TOKEN_JSON [sandbox]" >&2
  exit 2
fi
TOKEN="$1"
SANDBOX="${2:-keeper}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MANIFEST="$ROOT/data/demo-seed.json"
command -v nemohermes >/dev/null || { echo "nemohermes not found. Run this in the Ubuntu (WSL) window where NemoClaw is installed." >&2; exit 1; }
[ -f "$MANIFEST" ] || { echo "data/demo-seed.json not found. Load the demo inbox first (demo/SEED_AND_RESET.md)." >&2; exit 1; }

echo "== 1/3 Google token"
bash "$ROOT/agent/openshell/install-google-token.sh" "$TOKEN" "$SANDBOX"

echo "== 2/3 seed manifest"
# OpenShell uploads from /mnt/c can arrive as zero bytes, so stage a copy first.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 700 "$STAGE"
install -m 600 "$MANIFEST" "$STAGE/demo-seed.json.new"
cmp -s "$MANIFEST" "$STAGE/demo-seed.json.new" || { echo "manifest staging copy differs" >&2; exit 1; }
nemohermes "$SANDBOX" exec -- mkdir -p /sandbox/keeper/data
nemohermes "$SANDBOX" upload "$STAGE/demo-seed.json.new" /sandbox/keeper/data/
nemohermes "$SANDBOX" exec -- chmod 600 /sandbox/keeper/data/demo-seed.json.new
nemohermes "$SANDBOX" exec -- mv -f /sandbox/keeper/data/demo-seed.json.new /sandbox/keeper/data/demo-seed.json
echo "Seed manifest installed at /sandbox/keeper/data/demo-seed.json."

echo "== 3/3 network policy: Gmail and Calendar for Keeper, sending blocked"
nemohermes "$SANDBOX" policy add --from-file "$ROOT/agent/openshell/policies/keeper-google.yaml" --yes

echo
echo "Done. Check that Keeper can reach Gmail from inside the sandbox:"
echo "  nemohermes $SANDBOX exec -- /sandbox/keeper/bin/keeper health adapters --live --provider gmail"
