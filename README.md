# Keeper

[![tests](https://github.com/SiddarthShekhar1994/nebius-x-nvidia/actions/workflows/tests.yml/badge.svg)](https://github.com/SiddarthShekhar1994/nebius-x-nvidia/actions/workflows/tests.yml)

Keeper is a personal assistant for students, built for the Personal AI track of the Nebius x NVIDIA Global AI Hackathon. It reads a student's inbox, keeps track of internship applications, puts deadlines on the calendar and drafts replies for the student to send. It never sends mail itself. When you correct it in chat, it writes the correction down as a skill, so it doesn't make the same mistake twice.

Keeper runs as a Hermes agent inside an NVIDIA OpenShell sandbox, set up with NemoClaw. Every model call goes to NVIDIA Nemotron 3 (Nano, Super and Ultra) on Nebius Token Factory.

## What it does

- Sorts each email into junk, actionable, due or newsletter and labels it in Gmail.
- Flags scam patterns: upfront fees, pressure to act today, unrealistic pay, and career mail from free webmail addresses.
- Tracks applications. An email that names a company and role you track moves the application from found to applied, interview, or a decision, and the email is kept as evidence.
- Puts explicit deadlines on Google Calendar. When a deadline is extended, it moves the same event instead of adding a second one.
- Saves reply drafts in Gmail for you to edit and send.
- Learns from corrections: `/teach` has Nemotron 3 Ultra turn your latest correction into a skill, and `/keep` installs it once you approve.

## How Keeper uses Nemotron and Token Factory

Keeper's router (`router/`) picks a model for each job, calls Token Factory's OpenAI-compatible API and logs every call to SQLite: the model requested, the model that answered, tokens, latency and cost.

| Job | Model | Why |
| --- | --- | --- |
| Sorting every email, scoring radar results | Nemotron 3 Nano | It runs on every message, so it has to be cheap and quick. Its answer is a small JSON object that Keeper validates. |
| Reply drafts, and the agent's chat and tool calls | Nemotron 3 Super | A person reads and sends the drafts, and the agent needs dependable tool calls. |
| Writing skills from corrections, planning | Nemotron 3 Ultra | Rare jobs whose output becomes part of Keeper's instructions, so they have to be right. |

Where Token Factory helped:

- One endpoint and one key serve all three models, so routing by task is a lookup table rather than three deployments. Moving a job to another model is one environment variable (`KEEPER_MODEL_NANO`, `KEEPER_MODEL_SUPER`, `KEEPER_MODEL_ULTRA`).
- Tool calling worked on the first try with all three models, and the model that answered always matched the one we asked for.
- Calls were cheap enough to test freely. Our first smoke test, six task types across all three models, cost $0.0038.

`keeper router summary` totals the calls per model, and the [evidence dashboard](dashboard/README.md) shows the same numbers. Nemotron 3 reasons before it answers, and those tokens count against `max_tokens`, so every call through Keeper's router gets 4,096 tokens of room.

## Security

The OpenShell sandbox denies network access by default. Keeper adds two policies:

- `keeper-tokenfactory`: only the sandbox's Python runtime, which runs Keeper's code, can reach Token Factory, and only its chat completions and models endpoints.
- `keeper-google`: only the Gmail and Calendar calls Keeper makes are allowed: reading messages, adding labels, creating drafts, and reading and writing events on the primary calendar. Sending mail and sending drafts are denied.

The rules that matter most stay outside the model. Keeper never junks anything that might be career mail, the scam checks are plain patterns, and a deadline only reaches the calendar when the email gives an explicit date and names an opportunity you track. Keeper reads email text as data, never as instructions.

## Try it

### The live demo page

You need Python 3.11 or newer and a Token Factory key. From the repository root:

```bash
pip install -r requirements.txt
read -rs TOKENFACTORY_API_KEY && export TOKENFACTORY_API_KEY   # paste the key; it isn't shown
python -m space.server
```

Open http://localhost:7860. You can play the demo story (one internship, five emails) or send Keeper an email of your own. It runs Keeper's real code with live Nemotron calls. Gmail and Calendar are stand-ins kept in memory, so you don't connect an account and nothing is sent. See [space/README.md](space/README.md).

### The tests

No keys needed:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
```

They cover triage parsing and rules, the tracker, deadlines, drafts, the Gmail and Calendar adapters, the router, and the demo story end to end with stand-ins. CI runs them on every pull request and push to `main`: Python 3.12 and 3.13 on Ubuntu and 3.14 on Windows, plus pyflakes and a shell syntax check.

### The full agent

This runs on Linux or WSL and needs a throwaway Google account:

1. Install NemoClaw with Hermes and connect it to Token Factory: [docs/phase1-nemoclaw.md](docs/phase1-nemoclaw.md).
2. Give Keeper's router its key with `agent/openshell/install-key-file.sh`. Then authorize the demo Gmail account and load the synthetic inbox: [docs/google-authorization.md](docs/google-authorization.md) and [demo/SEED_AND_RESET.md](demo/SEED_AND_RESET.md).
3. Copy Keeper into the sandbox with `bash agent/deploy.sh`.
4. Sort the inbox ([triage/README.md](triage/README.md)), then play the demo story ([demo/STORY.md](demo/STORY.md)).

## Status

- **Working in the sandbox:** triage and Gmail labels on the 75-email synthetic inbox, the application tracker, reply drafts saved in Gmail, and learning from corrections. Nano sorted 74 of 75 emails correctly, extracted every deadline exactly, and sent no career email to junk. See the [triage guide](triage/README.md) and the [memory and skills report](docs/phase2-item3-report.md).
- **Calendar deadlines:** built and tested offline. `agent/demo-story.sh` runs them against the demo calendar. See [docs/calendar-adapter.md](docs/calendar-adapter.md).
- **Opportunity radar:** `keeper radar run` previews discovery offline. Live search needs a Tavily key, and sending the digest needs Telegram. See [docs/radar-discovery.md](docs/radar-discovery.md).
- **Not finished:** Telegram delivery, scheduled runs, and the network lockdown demo.

## Layout

| Folder | What goes in it |
| --- | --- |
| `agent/` | The `keeper` sandbox: persona, the `keeper` command, deploy and OpenShell setup scripts |
| `triage/` | Sorts mail into junk, actionable, due and newsletter with Nano, plus career and scam checks |
| `actions/` | `keeper act`: tracker, calendar deadlines and reply drafts for saved triage results |
| `keeper_core/` | Records, database migrations, the application tracker, deadlines, drafts and radar logic |
| `integrations/` | Gmail, Calendar, Telegram and Tavily adapters, and health checks |
| `router/` | Picks Nano, Super or Ultra per task and logs every call to SQLite |
| `skills/` | Keeper's skills as markdown, plus skills learned from corrections |
| `memory/` | Long-term memory store (markdown files on the sandbox's persistent volume) |
| `dashboard/` | Evidence report: triage, tracker, model usage and cost, evaluation |
| `demo/` | Synthetic demo inbox, seeding tools and the demo story. Never real inbox content. |
| `space/` | The live demo page: the demo story and visitors' own emails through Keeper's code on live Nemotron, with stand-in Gmail and Calendar |
| `docs/` | Setup steps, plans, phase reports and the [submission text](docs/submission.md) |
| `scripts/` | Developer utilities, including Google authorization and the Token Factory probe |
| `tests/` | Offline tests |

## Feedback

[NOTES_FEEDBACK.md](NOTES_FEEDBACK.md) logs everything that broke or was confusing in Token Factory, NemoClaw, OpenShell, Hermes and Nemotron while building this.

## License

MIT. See [LICENSE](LICENSE).
