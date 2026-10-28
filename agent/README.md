# agent

Keeper runs on the Hermes agent inside NVIDIA OpenShell, set up with NemoClaw. This folder has no custom agent loop. Instead it holds what gets layered onto Hermes in the `keeper` sandbox.

| File | What it does |
| --- | --- |
| `SOUL.md` | Keeper's persona and hard rules. `deploy.sh` puts it at the top of `/sandbox/.hermes/runtime/SOUL.md` (the one Hermes reads), `/sandbox/.hermes/SOUL.md` and `/sandbox/SOUL.md` (merge code in `skills/soul.py`) |
| `bin/keeper` | Keeper's command inside the sandbox (`keeper memory`, `keeper skills`, `keeper router`, `keeper health`, `keeper triage`, `keeper apps`) |
| `deploy.sh` | Copies code, skills and persona into the sandbox, turns on Hermes's skill write-approval gate and adds the `/teach`, `/keep`, `/drop` and `/apps` quick commands. Rerun it after changes. |
| `restore.sh` | Puts memory, the router log, the key file and learned skills back into a re-created sandbox |
| `openshell/tokenfactory-keeper-v1.yaml` | OpenShell provider profile that gives the router all three Nemotron models without putting the key in the sandbox |
| `openshell/policies/keeper-tokenfactory.yaml` | Network policy preset that opens Token Factory to Keeper's Python only |
| `openshell/setup-tokenfactory.sh` | One-time setup: import the profile, create and attach the provider, apply the policy |
| `openshell/install-google-token.sh` | Install the demo Google OAuth token from WSL into the private sandbox secrets directory |
| `openshell/setup-telegram.sh` | Configure the Telegram bot and Hermes gateway through NemoClaw on the WSL host |
| `openshell/install-provider-key.sh` | Install private Telegram and Tavily keys for Keeper's direct API adapters |

Steps and proof checks are in `docs/phase2-memory-skills.md`. Install and sandbox setup are in `docs/phase1-nemoclaw.md`; Google authorization is in `docs/google-authorization.md`; Telegram and Tavily setup is in `docs/telegram-tavily-adapters.md`. Run the [integration health and gateway access checks](../docs/integration-health-and-access.md) after deployment.

## Application tracker

Use `keeper apps --db /path/to/keeper.db list` to see current applications and
`keeper apps --db /path/to/keeper.db history APPLICATION_ID` for every recorded
status change, source email, evidence, and correction. Reading does not create a
database.

An operator can apply a reviewed correction with:

    keeper apps --db /path/to/keeper.db correct APPLICATION_ID \
      --status applied --actor user:1 --reason "Verified application" \
      --evidence "User reviewed the submission receipt" \
      --correction-id fix:receipt-1 --approve

For `--status decision`, also provide `--outcome` (for example `offered` or
`rejected`). `--approve` explicitly authorizes the change. Reuse the same
correction ID on retries; a different correction needs a new ID. Corrections
preserve prior history and hold subsequent automatic transitions for review.

`/apps` in Telegram runs `keeper apps telegram` through the existing Hermes
quick-command gateway. It reads the same product SQLite database and returns a
short plain-text count and list. It does not create or migrate a database or
change application state. Run `keeper health permissions` as the gateway user
after deployment to verify that the command is configured and the product
database is readable.
