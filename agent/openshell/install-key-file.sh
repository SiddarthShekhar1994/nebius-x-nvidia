#!/usr/bin/env bash
# Fallback if OpenShell won't swap its placeholder for the real key: put the
# Token Factory key in /sandbox/keeper/secrets/tokenfactory.key (mode 600),
# which the router reads before the placeholder. Trade-off: the key now lives
# inside the sandbox, readable by anything running as the sandbox user.
# Rerun it to rotate the key; the Google token and other keys in secrets/ stay.
#   bash agent/openshell/install-key-file.sh [sandbox]
export PATH="$HOME/.local/bin:$PATH"
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh" >/dev/null 2>&1; fi
set -euo pipefail
SANDBOX="${1:-keeper}"
STAGE="$(mktemp -d)"   # Linux side: uploads from /mnt/c arrive as zeros
trap 'rm -rf "$STAGE"' EXIT
chmod 700 "$STAGE"
if [ -z "${TOKENFACTORY_API_KEY:-}" ]; then
  read -rsp "Token Factory API key (input hidden): " TOKENFACTORY_API_KEY; echo
fi
[ -n "$TOKENFACTORY_API_KEY" ] || { echo "No key entered; nothing changed." >&2; exit 1; }
( umask 077; printf '%s' "$TOKENFACTORY_API_KEY" > "$STAGE/tokenfactory.key.new" )
unset TOKENFACTORY_API_KEY
nemohermes "$SANDBOX" exec -- mkdir -p /sandbox/keeper/secrets
nemohermes "$SANDBOX" exec -- chmod 700 /sandbox/keeper/secrets
nemohermes "$SANDBOX" upload "$STAGE/tokenfactory.key.new" /sandbox/keeper/secrets/
nemohermes "$SANDBOX" exec -- chmod 600 /sandbox/keeper/secrets/tokenfactory.key.new
nemohermes "$SANDBOX" exec -- mv -f /sandbox/keeper/secrets/tokenfactory.key.new /sandbox/keeper/secrets/tokenfactory.key
echo "Key file installed. Test with:"
echo "  nemohermes $SANDBOX exec -- /sandbox/keeper/bin/keeper router smoke"
