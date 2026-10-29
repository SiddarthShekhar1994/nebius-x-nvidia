# Keeper

Keeper is an always-on personal assistant for students, built for the Personal AI track of the Nebius x NVIDIA Global AI Hackathon. It reads a demo inbox and calendar, keeps an eye out for internships and hackathons, and reports back over Telegram. When you correct it, it writes the correction down as a skill so it doesn't make the same mistake twice.

It runs as a Hermes agent inside NVIDIA OpenShell, set up with NemoClaw. Inference goes to Nemotron 3 models (Nano, Super and Ultra) on Nebius Token Factory.

## Status

- **Feasibility and the working assistant (phases 1 and 2):** done: sandbox, model router, memory, and learning from corrections. See the [phase 1 report](docs/phase1-report.md) and the [memory and skills report](docs/phase2-item3-report.md).
- **Inbox triage:** 74 of 75 correct live with Nemotron 3 Nano on the synthetic demo inbox (prompt triage-v3): every deadline extracted exactly, no career email ever sent to junk, and every scam email flagged by the built-in checks. Labels go to Gmail only with `--apply`. See the [triage guide](triage/README.md) and the [acceptance report](docs/phase4-triage-report.md).
- **Tracker, calendar deadlines and reply drafts:** built and tested offline with fakes. `keeper act` runs them on saved triage results, and `keeper apps` shows the tracker. See [actions/README.md](actions/README.md) and [docs/calendar-adapter.md](docs/calendar-adapter.md).
- **Opportunity radar:** search, scoring, eligibility and deduplication are built; it has no command yet. See [docs/radar-discovery.md](docs/radar-discovery.md).
- **Evidence dashboard:** a self-contained HTML report generated from Keeper's databases. See [dashboard/README.md](dashboard/README.md).
- **Still to do:** live runs against the demo Gmail account, the OpenShell network lockdown with the injection demo, scheduling, and the demo video.

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
| `demo/` | Synthetic demo inbox, seeding tools and scenarios. Never real inbox content. |
| `docs/` | Setup steps, plans and phase reports |
| `scripts/` | Developer utilities, including Google authorization and the Token Factory probe |
| `tests/` | Offline tests: `python -m unittest discover -s tests` |

## Setup

1. Install NemoClaw with the Hermes agent and connect it to Token Factory by following [docs/phase1-nemoclaw.md](docs/phase1-nemoclaw.md).
2. For local development, copy `.env.example` to `.env` and fill in your keys. Inside the sandbox, the OpenShell gateway holds the keys instead.

## Feedback

[NOTES_FEEDBACK.md](NOTES_FEEDBACK.md) logs everything that broke or was confusing in Token Factory, NemoClaw, OpenShell, Hermes and Nemotron while building this.

## License

MIT. See [LICENSE](LICENSE).
