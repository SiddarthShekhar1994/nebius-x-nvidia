# Devpost submission text

Copy each section into the matching field on Devpost.

## Elevator pitch

Keeper reads a student's inbox, tracks internship applications, puts deadlines on the calendar and drafts replies. It runs on NVIDIA Nemotron 3 inside an OpenShell sandbox, and it can't send mail.

## Track

Personal AI

## Project story

### Inspiration

Applying for internships comes with a second job in your inbox. There are application receipts, interview invitations, take-home deadlines that move, club newsletters, and a steady trickle of fake recruiters asking for a fee. Miss one email and you can lose an offer. We wanted an assistant to do the bookkeeping (sorting, tracking, calendar entries, first drafts of replies) without ever being trusted to send anything on its own.

### What it does

Keeper watches a Gmail inbox. It:

- sorts each email into junk, actionable, due or newsletter, and labels it in Gmail
- flags scam patterns: upfront fees, pressure to act today, unrealistic pay, and career mail from free webmail addresses
- tracks applications: an email that names a company and role you track moves the application from found to applied, interview, or a decision, and the email is kept as evidence
- puts explicit deadlines on Google Calendar, and moves the same event when a deadline is extended instead of adding a second one
- saves reply drafts in Gmail for you to edit and send
- learns from corrections: tell it what it got wrong, and `/teach` turns that into a skill it follows once you approve it

It never sends mail. Drafts stay drafts, and the sandbox's network policy blocks Gmail's send endpoints.

### How we built it

- Keeper is a Hermes agent running inside an NVIDIA OpenShell sandbox, set up with NemoClaw. Hermes provides the chat loop, memory, skills and quick commands.
- All inference runs on NVIDIA Nemotron 3 through Nebius Token Factory. A small router picks the model for each job: Nano sorts every email because it's cheap and fast, Super writes replies and drives the agent's tool calls, and Ultra handles rare jobs that need care, like writing a skill from a correction. Every call is logged with the requested and answering model, tokens, latency and cost.
- The sandbox denies network access by default. We added two policies. One lets only the sandbox's Python runtime reach Token Factory. The other allows only the Gmail and Calendar calls Keeper makes and denies sending mail and drafts.
- The rules that matter most stay outside the model. Keeper never junks anything that might be career mail, the scam checks are plain patterns, and a deadline only reaches the calendar when the email has an explicit date and names an opportunity you track.
- We tested on a synthetic 75-email inbox loaded into a throwaway Gmail account. Nemotron 3 Nano sorted 74 of 75 correctly and extracted every deadline exactly. The test suite runs on every pull request on Linux and Windows.
- The repository also has a demo page anyone can run locally with their own Token Factory key. It plays the internship story through Keeper's real code with live Nemotron calls, using stand-ins for Gmail and Calendar.

### Challenges we ran into

- Reasoning tokens. Nemotron 3 models reason before answering, and those tokens count against `max_tokens`. Small caps gave us empty or cut-off answers until we raised them to 4,096.
- Nano sometimes returned a list as a comma-separated string, so the parser accepts both. Identical inputs at temperature 0 didn't always get identical labels, which is one more reason the safety rules aren't left to the model.
- OpenShell intercepts TLS, and Google's Python client couldn't get through the sandbox's proxy, so we wrote a small Gmail and Calendar client on Python's standard library.
- A Docker Desktop crash left our sandbox in a state OpenShell treats as terminal. We wrote up a recovery path and rebuilt the sandbox from backups.
- Keeper's persona silently didn't load. Hermes inside NemoClaw read its persona file from a path neither project documents, and we only found it by tracing Hermes with a profiler.

### Accomplishments that we're proud of

- Keeper works end to end on a real Gmail account: it sorts and labels mail, tracks applications and saves drafts, and the network policy blocks it from sending.
- 74 of 75 correct on the test inbox with the smallest model.
- Learning from corrections goes through Hermes's approval step, so nothing a model writes becomes one of Keeper's instructions until a person approves it.

### What we learned

- Pick the model per job. Most of an inbox only needs Nano. Super and Ultra are worth their cost for the few jobs where a person reads the output or it becomes an instruction.
- Keep the guardrails outside the model. The parts we trust most are plain code and the network policy.

### What's next

- Telegram delivery and a daily summary on a schedule.
- Turning on the opportunity radar live, so Keeper finds internships as well as tracking them.
- A network lockdown demo: an email that tries to make Keeper send data out, and the sandbox refusing.

## Built with

Python, NVIDIA Nemotron 3 (Nano, Super, Ultra), Nebius Token Factory, NVIDIA OpenShell, NVIDIA NemoClaw, Hermes Agent, Gmail API, Google Calendar API, SQLite, GitHub Actions

## Links

- Code: https://github.com/SiddarthShekhar1994/nebius-x-nvidia
- Working demo (test build): the same repository. The "Try it" section of the README runs the demo page locally with a Token Factory key.

## What changed during the submission period

Keeper was built from scratch for this hackathon. The first commit is from Oct 15, 2026.

## Feedback on Nebius Token Factory, NVIDIA models and tools

The full log, with commands and exact errors, is in [NOTES_FEEDBACK.md](../NOTES_FEEDBACK.md).

**Nebius Token Factory**

- It worked well. The standard OpenAI SDK worked unchanged, tool calling worked on the first try with all three Nemotron 3 models, the model that answered always matched the one requested, and costs were tiny.
- The three Nemotron 3 model IDs follow three different naming styles and are case sensitive. The obvious guess for Nano returned nothing until we copied the ID from `GET /v1/models`.
- Reasoning tokens count against `max_tokens`, and Nano spent 174 to 281 tokens on one-word classifications. The model cards should say so, and say whether reasoning can be turned off per request and whether `response_format` with a JSON schema is supported.
- Identical inputs at temperature 0 sometimes got different labels. If that's expected for reasoning models, the model card should say so.

**NemoClaw, OpenShell and Hermes**

- Token Factory isn't a built-in inference provider in NemoClaw. It goes through "Other OpenAI-compatible endpoint", which is marked "tested with limitations". On WSL, pressing Enter in the express setup picks a local Ollama model.
- A custom OpenShell provider for Token Factory attached and its traffic was allowed, but the key placeholder was never swapped for the real key. Token Factory returned 401, and no log line said why. We fell back to a key file inside the sandbox.
- Files uploaded into the sandbox from the Windows drive (`/mnt/c`) arrived as the right size but all zero bytes, with no error.
- After a Docker Desktop crash, the sandbox stayed in the Error phase with no supported way out. Stale lock files from before a WSL reboot also made every command hang.
- Inside NemoClaw, Hermes read its persona from a runtime path neither project documents, so the persona was silently ignored. When the persona file isn't found, Hermes should log the path it tried.
- Hermes only checks a skill's format at approval time, so a bad write looks fine until the whole batch rolls back. Quick commands can't take arguments.
