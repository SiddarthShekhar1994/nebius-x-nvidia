# Keeper build plan

> Superseded for October 28 to 30 by the final-stretch section at the top of [KEEPER_MASTER_DEVELOPMENT_PLAN.md](KEEPER_MASTER_DEVELOPMENT_PLAN.md), which covers all five teammates.

Six days, two builders plus Claude. Day 1 is October 24, and day 6 is submission. Phase 1 and phase 2 items 1 to 3 are done: the sandbox, the model router, memory, and learning from corrections.

**The product in one line:** Keeper runs a student's internship pipeline from first email to decision. It finds opportunities, checks them, plans the deadlines, drafts replies, tracks every application, learns from corrections, and runs in a sandbox that blocks hijack attempts.

## 1. Tech stack

Everything already in use stays as it is. New pieces are marked *new*.

| Layer | Choice | Why |
| --- | --- | --- |
| Agent | Hermes Agent 0.21.3 in an NVIDIA OpenShell sandbox, set up with NemoClaw | Already running. It's the stack the hackathon is about |
| Models | Nemotron 3 Nano, Super and Ultra on Nebius Token Factory | Nano for sorting and scoring, Super for drafts and tool calls, Ultra for skill writing |
| Model routing | `router/` with every call logged to SQLite | Already built. Gives us the cost and latency numbers |
| Language | Python 3.11+, small modules, stdlib where possible | Matches the existing code |
| Gmail and Calendar | *new* `google-api-python-client` and `google-auth-oauthlib`. Scopes: `gmail.modify`, `gmail.compose`, `calendar.events`, plus `gmail.insert` for the demo seeding script only. **No `gmail.send`** | Keeper can't send mail even by mistake, because it never has the permission |
| Google login | *new* OAuth "Desktop app" flow run once on Windows. The token file goes into `/sandbox/keeper/secrets/` with a script, like the Token Factory key | No browser inside the sandbox |
| Telegram | *new* Hermes's built-in Telegram gateway for chat and `/teach`. Keeper posts summaries straight to the Bot API (`sendMessage`) | Hermes already does the chat side. Direct sends keep summaries reliable |
| Web search | *new* Tavily API, called at run time | Required by the spec. NemoClaw has a Tavily preset |
| Scheduling | *new* Hermes cron jobs: triage every 30 minutes, radar daily | Runs inside the always-on gateway. Day 1 includes a short test of whether a cron job can run a plain command (see the plan) |
| Keeper's data | `memory/` markdown for profile and facts. *new* `data/keeper.db` (SQLite) for emails seen, opportunities and applications | Markdown for things the agent reads, SQLite for things we query and chart |
| Network lockdown | *new* OpenShell policy that allows only Token Factory, Tavily, Google and Telegram. Everything else gets a 403 that shows in the audit log | Item 6, and the core of the security demo |
| Eval | *new* `eval/` Python scripts over a labeled demo inbox | Accuracy numbers instead of "it works" |
| Dashboard | *new* one static HTML page in `dashboard/` built by a Python script from `router.db`, `keeper.db` and the eval results. Charts with Chart.js from a CDN | No server or framework to break during the demo |

## 2. Features

Grouped by pipeline step. **Must** is the demo, **stretch** only happens if the must items work by the end of day 4.

| Step | Feature | Priority |
| --- | --- | --- |
| Find | Triage unread mail every 30 minutes into junk, actionable, due or newsletter (Nano) | Must |
| Find | Career mail (internships, hackathons, admissions, programs) is never junk: a keyword and sender check after the model, which overrides it | Must |
| Find | Daily radar: Tavily searches built from `profile.md`, Nano scores 1 to 10, deduped against memory, top 5 to Telegram with one-line reasons | Must |
| Find | Eligibility check against the profile (year, major, location, work authorization), with the reason when something is skipped | Must |
| Verify | Scam check on career mail: upfront fees, a free-mail sender claiming a company, pressure to act fast, too-good pay. Flags with a reason | Must |
| Verify | Real deadline from the application page: follow the link and read the closing date | Stretch 1 |
| Plan | A calendar event for each deadline. "Deadline extended" moves the existing event instead of adding a second one | Must |
| Plan | Backward plan: resume and cover letter milestones before the deadline | Stretch 2 |
| Draft | Reply drafts for actionable mail, saved as Gmail drafts, never sent (Super) | Must |
| Draft | Cover letter draft as a Google Doc | Stretch 3 |
| Track | Application tracker built from the inbox: found, applied, interview, decision. Confirmation, interview and rejection emails move items along | Must |
| Track | `/apps` in Telegram shows the tracker | Must |
| Learn | Every triage decision has a one-line reason. Corrections become skills through `/teach` and `/keep` (already built) | Must |
| Learn | `/teach` and `/keep` working from Telegram (fix the gateway user's file permissions) | Must |
| Protect | Network lockdown, with blocked requests in the audit log | Must |
| Protect | Prompt-injection email in the demo inbox: Keeper flags it, and the sandbox blocks the exfiltration attempt | Must |
| Prove | Labeled demo inbox of about 30 emails over two weeks | Must |
| Prove | Eval: accuracy per bucket, career-to-junk count, Nano vs Super vs Ultra on accuracy, cost and speed | Must |
| Prove | Score again after a correction and show it going up | Must |
| Prove | "What you would have missed" replay: two weeks with and without Keeper | Must |
| Prove | Dashboard: triage results, tracker, cost per model, eval chart, replay, blocked requests | Must |

## 3. Order of design

Contracts first, so the two builders can work in parallel without waiting on each other.

1. **Data contracts (day 1, morning).** Agreed and written down in `docs/contracts.md` before anyone writes feature code:
   - Demo email JSON (id, date, from, subject, body, label, career, deadline, attack, note)
   - Triage result: bucket, reason, career, scam, deadline, confidence
   - Opportunity: title, org, url, deadline, score, reason, eligible, skip_reason
   - Application: id, opportunity, status (found, applied, interview, decision), history of status changes with the email that caused each
2. **Pure logic with no network**, tested offline: triage prompt and career override, scam rules, deadline parsing, tracker state changes, eligibility rules.
3. **Adapters**: Gmail, Calendar, Telegram, Tavily. Each one is a small module with a fake for tests.
4. **Commands** that wire logic and adapters: `keeper triage run`, `keeper radar run`, `keeper apps`.
5. **Scheduling and chat**: Hermes cron jobs and Telegram.
6. **Lockdown last**, because once it's on, every new host has to be added on purpose.
7. **Eval, replay and dashboard** read the same contracts, so they can be built from day 1 against the demo inbox alone.

## 4. In scope, and the cut list

**In scope:** everything marked Must above.

**Out of scope, not even if there's time:** study planner, GPA or class tracking, a mobile app, voice, multiple users, real inbox data, sending any email.

**If we're running out of time, cut in this order** (top goes first):

1. Stretch 3: cover letter as a Google Doc
2. Stretch 2: backward plans
3. Stretch 1: deadline from the application page
4. `/apps` in Telegram (keep the tracker on the dashboard)
5. Score after a correction (keep the main eval)
6. `/teach` and `/keep` from Telegram (show learning in the terminal chat instead)
7. Eligibility check (keep scoring and dedupe)
8. Ultra in the model comparison chart (keep Nano vs Super)

**Never cut:** triage with the career rule, Gmail drafts, calendar deadlines, the Telegram summary, the radar, the tracker, the scam check, the lockdown with the attack email, the eval, and the backup video.

## 5. Full build plan

**You** = Siddarth, on Windows with the sandbox. **Teammate** = works from the repo with their own Token Factory key and doesn't need the sandbox. **Claude** = writes code for both and keeps the pieces compatible.

### Day 1: contracts and access

- **You:** create the throwaway demo Gmail, set up the Google Cloud project with the Gmail and Calendar APIs and a "Desktop app" OAuth client, create the Telegram bot, get a Tavily key. None of these go in the chat or the repo.
- **Claude:** `docs/contracts.md`; the Google login script that runs on Windows and the script that installs the token into the sandbox; Gmail, Calendar and Telegram adapters with fakes; a test of whether a Hermes cron job can run `keeper triage run` directly.
- **Teammate:** repo setup, `python -m router smoke` passing, start the labeled demo inbox. It must include the scam email, the injection email, a confirmation, an interview invite, a rejection and a "deadline extended" email.
- **End of day check:** the Google login works from Windows, and the demo inbox has its first 15 emails.

### Day 2: triage (item 4, first half)

- **Claude:** triage logic (Nano bucket with reason, career override, scam check, deadline extraction), plus a seeding script that loads the demo inbox into Gmail with `insert`, which never sends.
- **You:** seed the demo Gmail, run `keeper triage run` in the sandbox, check the results in Gmail and Telegram.
- **Teammate:** finish the demo inbox at 30 emails, build the eval runner against the triage function.
- **End of day check:** one triage run sorts the seeded inbox and posts a Telegram summary, and the eval prints accuracy.

### Day 3: drafts, calendar and tracker (item 4, second half)

- **Claude:** Gmail drafts for actionable mail (Super), calendar events with the "deadline extended" update, the application tracker in `keeper.db`, and file permission fixes so `/teach` and `/keep` work from Telegram.
- **You:** turn on the 30-minute schedule, test `/teach` from Telegram, write `profile.md`.
- **Teammate:** model comparison (Nano, Super, Ultra) and its chart, then the score-after-correction run.
- **End of day check:** item 4 is fully done. Stop for review.

### Day 4: radar (item 5) and the backup video

- **Claude:** Tavily searches from the profile, Nano scoring, eligibility check, dedupe against memory, top 5 to Telegram, deadlines to the calendar, and `/apps`.
- **You:** run the radar, check that the results are real and current, **record a backup demo video** of triage, the tracker and the radar.
- **Teammate:** the "what you would have missed" replay.
- **End of day check:** item 5 done, and a backup video exists. Stop for review. If any must item is behind, start using the cut list.

### Day 5: lockdown (item 6) and the dashboard

- **Claude:** the OpenShell policy (Token Factory, Tavily, Google and Telegram only), the injection email demo path, and a check that reads the audit log for 403s.
- **You:** apply the policy, run everything again to make sure nothing legitimate is blocked, and show a blocked request.
- **Teammate:** the dashboard page with triage results, the tracker, cost per model, the eval chart, the replay and blocked requests.
- **Stretch, only if everything above works:** stretch 1, then 2, then 3.
- **End of day check:** item 6 done. Stop for review.

### Day 6: demo and submission

- **Everyone:** a full run on a freshly seeded inbox, then fix only what breaks.
- **Video, 3 minutes:**
  1. The attack email fails (the hook).
  2. Inbox before and after.
  3. A radar hit goes onto the calendar.
  4. The tracker moves after a confirmation email.
  5. A live correction changes the next run.
  6. The dashboard: accuracy, cost per model, and the replay.
- **Teammate:** README with setup steps, the architecture and the results. **Claude:** final `NOTES_FEEDBACK.md`. **You:** submit, then check the links.

## Rules that don't change

- Only the throwaway Gmail account, never a real inbox, in data or on video.
- Keeper never sends email. It only drafts.
- No keys, tokens or OAuth files in the repo or the chat.
- Teammate works on their own branch and opens pull requests into `main`.
- Every item ends with a review stop.
