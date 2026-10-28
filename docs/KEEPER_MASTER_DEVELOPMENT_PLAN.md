# KEEPER MASTER DEVELOPMENT PLAN

**Version 1.0 — Five-Developer Hackathon Execution Roadmap**  
**Project:** Keeper — Secure, Adaptive Student Career Agent  
**Event:** Nebius × NVIDIA Global AI Hackathon  
**Sprint:** October 24–30, 2026. Submission deadline confirmed: October 30 (confirm the time zone).  
**Team:** Rishi, Sid, Zayd, Vi, Sanjay  
**Status:** Foundation partly implemented; integrations, end-to-end features, evaluation, security proof, and submission pending.

> **Operating principle:** Ship one secure, reproducible, convincing end-to-end workflow before expanding the feature surface. The project wins or loses on demonstrated behavior, not the number of checkboxes.

---

## FINAL STRETCH: OCTOBER 28 TO 30 (current assignments)

This section replaces the day-by-day table further down for the last three days. Lanes follow the ownership matrix in 0.3.

### Where we are on October 28

| Piece | Status |
| --- | --- |
| Triage with career protection and scam warnings | Done. 74 of 75 (98.7%) live on the demo fixture with prompt triage-v3 (#9, #10). Deadlines 19/19 exact |
| Evaluation of triage | Done for Nano: `python -m triage compare` |
| Gmail, Calendar, Telegram, Tavily adapters | Code and fakes exist. No live test yet |
| Triage on the real demo Gmail | Not done. Needs the seeded throwaway account |
| Tracker, Gmail drafts, calendar deadlines | Building blocks exist (`keeper_core/workflow.py`, adapters). Nothing connects them to triage results yet |
| Telegram summary | Not started |
| Radar | Not started |
| Network lockdown and attack email | Not started. Only the Token Factory policy exists |
| Dashboard and replay | Not started (`dashboard/` is empty) |
| Backup video | Not started |

### How the action step fits together

One command, `keeper act`, reads saved triage results and does three things. Each owner builds one module behind the same small interface, so nobody waits on anyone else:

| Module | Owner | Does | Never does |
| --- | --- | --- | --- |
| `actions/tracker.py` | Rishi | Confirmation, interview and rejection emails move an application forward, once per email | Changes a status without the email that caused it |
| `actions/drafts.py` | Zayd | One Gmail draft per actionable email (Super), once per email | Sends anything |
| `actions/deadlines.py` | Vi | One calendar event per verified deadline. An extension moves the existing event | Creates a second event for the same application |
| `keeper act` command | Rishi | Calls the three modules in order and prints a summary | |

Claude drafts the shared interface and an empty version of each module first thing on October 28, so all three can start from the same code.

### Assignments

**Sid (sandbox, OAuth, security)**
- Oct 28: create the Google setup (throwaway Gmail, Cloud project with Gmail and Calendar APIs, one OAuth client for Keeper and one for seeding). Seed the 75 demo emails, run triage on the real inbox (dry run, then `--apply`), screenshot the labeled inbox.
- Oct 29: deploy to the sandbox, run `keeper triage run` and `keeper act` there, apply the lockdown policy, send the attack email and capture the blocked request in the audit log, test the Telegram bot. Record the backup video in the evening.
- Oct 30: fresh seed, full run, final video, submit.

**Rishi (backend, tracker, orchestration)**
- Oct 28: `actions/tracker.py` and the `keeper act` command. Telegram summary after each run ("12 new: 3 due, next deadline Fri; 4 actionable; 2 scam warnings").
- Oct 29: merge Zayd's and Vi's modules into `keeper act`, run it end to end on the fixture, review PRs. Learn the deploy steps with Sid so two people can run the demo.

**Zayd (triage, drafts, benchmarking)**
- Oct 28: `actions/drafts.py`. Then run the 75 fixture emails through Super as well as Nano and record accuracy, cost and speed.
- Oct 29: finish drafts, help Rishi with the end-to-end run. If Sanjay is unavailable, take over the dashboard basics.

**Vi (radar, Tavily, Calendar)**
- Oct 28: `actions/deadlines.py`, including the extension case.
- Oct 29: `keeper radar run`: Tavily searches from `memory/profile.md`, Nano scores 1 to 10, dedupe against what's been seen, top 5 to Telegram.
- If Vi is unavailable: Rishi takes deadlines on Oct 29 and the radar is cut.

**Sanjay (evaluation, dashboard, demo)**
- Oct 28: the dashboard, one static HTML page built by a Python script with Chart.js: the 80% to 98.7% triage result, accuracy per bucket, cost per model from `router.db`.
- Oct 29: add the tracker, Zayd's model comparison and the blocked requests. Write the video script and shot list from the final demonstration sequence in Part VIII.
- If Sanjay is unavailable: Zayd builds a simpler dashboard and Sid writes the shot list.

**Claude (coding support, not an owner)**
- Oct 28: the shared interface for `actions/`, then help whichever module is behind. Review every PR before merge.
- Oct 29 morning: the OpenShell lockdown policy (Token Factory, Google, Telegram and Tavily only) and the attack email for the demo inbox, for Sid to apply.
- Oct 30: fixes from the full run and the final `NOTES_FEEDBACK.md`.

### Checkpoints

- **Oct 28, end of day:** labeled real inbox screenshot, `keeper act` works on the fixture with fakes, dashboard shows the triage numbers.
- **Oct 29, midday:** full run in the sandbox. If the radar isn't working by then, cut it.
- **Oct 29, end of day:** lockdown proof captured and the backup video recorded. After this, no new features, only fixes.
- **Oct 30:** fresh run, final video, README, submit.

**Cut order for these three days:** replay, then radar, then the model comparison, then Telegram summary polish. Never cut: triage on real Gmail, drafts, calendar deadline, tracker update, the lockdown with the attack email, the eval numbers, and the video.

All work goes on short-lived branches with pull requests into `claude/dreamy-tesla-7tv0ko`, with offline tests that use the fakes.

---

## PART 0 — PROJECT MISSION, BASELINE, AND DELIVERY RULES

### 0.1 Product definition

Keeper is an always-on career-opportunity agent for students. It finds relevant opportunities, classifies and checks incoming career email, tracks applications and deadlines, prepares unsent drafts, and learns from user corrections. It runs Hermes inside NVIDIA OpenShell/NemoClaw, uses Nebius Token Factory Nemotron models, and demonstrates that untrusted email cannot override its security boundaries.

**Core demo loop:** seeded inbox → classification and scam check → Telegram summary → Gmail draft and calendar deadline → tracker update → correction and improved subsequent decision → blocked injection → evaluation dashboard.

### 0.2 Repository inspection: verified baseline

Based on the supplied ZIP and `Keeper build plan.pdf`:

| Area | Current evidence | Action |
|---|---|---|
| Agent sandbox | `agent/`, deployment/restoration scripts, OpenShell policy files, Hermes persona | Preserve; validate on target machine |
| Nemotron router | `router/core.py`, `routes.py`, `calllog.py`, `pricing.py`; tier mapping and SQLite logs | Preserve; add end-to-end usage and comparison |
| Persistent memory | `memory/store.py`, CLI and tests | Preserve; connect to opportunity state and corrections |
| Correction learning | `skills/teach.py`, draft/keep/drop, Hermes skill definitions | Preserve approval gate; test Telegram gateway permissions |
| Existing tests | `tests/test_memory.py`, `test_persona.py`, `test_router.py`, `test_skills.py`, `test_teach.py` | Establish clean local CI baseline |
| Gmail/Calendar/Tavily/Telegram product adapters | Described in roadmap, not present as implemented modules in supplied snapshot | Build and test |
| Product data store, triage, radar, tracker | Specified but not implemented in supplied snapshot | Build and integrate |
| Evaluation and dashboard | Placeholder README/directories | Build |
| CLI | `agent/bin/keeper` dispatches `memory`, `skills`, `router` only | Extend without breaking existing commands |

**Known integration hazards from repository notes:** Hermes may read `SOUL.md` from `/sandbox/.hermes/runtime/`; Telegram gateway runs under a different user; Telegram sessions may need `/new` after persona changes; OpenShell model routing may rewrite requested model IDs; Hermes native chat usage is not automatically included in `router.db`. Validate these explicitly.

**Do not mark features complete solely because a PDF says they are complete.** Completion requires passing tests and a recorded working demonstration in the current environment.

### 0.3 Proposed ownership matrix

These are initial assignments, **not assertions about unverified individual specialties**. Confirm after a 20-minute skills/availability sync.

| Developer | Primary lane | Secondary lane | Deliverable ownership |
|---|---|---|---|
| **Sid** | Sandbox, Hermes, OpenShell, OAuth and service credentials | Security demo | Environment, permissions, policy, recovery |
| **Rishi** | Product backend, contracts, SQLite, workflow orchestration | Integration and review | End-to-end CLI, tracker, cross-module behavior |
| **Zayd** | Gmail triage, classification, draft pipeline | Model benchmarking | Inbox ingestion, labeling, draft reliability |
| **Vi** | Opportunity radar, Tavily, eligibility, Calendar | Scheduling | Search-to-calendar path |
| **Sanjay** | Evaluation fixtures, dashboard, replay and demo | Test automation | Reproducible evidence and submission assets |

**Rotation:** Every feature owner names a reviewer from another lane. Sid is not the only person who can run the demo; Rishi and one other teammate must learn the deployment procedure. Claude/Codex are coding assistants, not owners, reviewers, or substitutes for integration testing.

### 0.4 Priority classification

- **P0 — Demo-critical:** Contracts, functioning Gmail triage, safety rules, calendar, unsent drafts, tracker, radar, Telegram summary, network deny proof, eval, reproducible demo, submission.
- **P1 — Important:** Telegram `/apps`, correction demonstration, eligibility explanations, rich dashboard, replay.
- **P2 — Stretch:** Following application pages for deadline verification, backward-planned milestones, cover letter Google Doc, sophisticated UI.
- **Never build during this sprint:** Multi-user SaaS, production auth system, real student inbox processing, outbound email sending, mobile app, class/GPA planner, autonomous job applications.

### 0.5 Team workflow and non-negotiable standards

1. Protect `main`; use `feature/<area>-<short-name>` branches and small pull requests.
2. Agree on schemas and module boundaries before parallel implementation; one person owns each contract.
3. Use `python -m unittest discover tests` as the minimum regression gate; add integration suites as built.
4. Keep external services behind adapters; every adapter has a fake used in offline tests.
5. No API keys, OAuth tokens, inbox contents, or private profile data in commits, prompts, screenshots, or videos.
6. Never request `gmail.send` or implement a send endpoint; drafts require explicit user review.
7. Make side effects idempotent: replaying an email cannot create duplicate tracker rows, events, or drafts.
8. Every model output is untrusted until schema-validated; email and webpage content are untrusted input, not instructions.
9. Record every important decision and blocker in `docs/decisions.md` and `docs/blockers.md`.
10. Merge continuously, demo daily, freeze features before submission day, and retain a working backup recording.

---

# PART I — SHARED FOUNDATION AND INTERFACES

## PHASE 0 — REPOSITORY AUDIT, ENVIRONMENT, AND TEAM ALIGNMENT

**Priority:** P0 | **Lead:** Rishi + Sid | **Target:** Day 1, first 2 hours  
**Depends on:** Existing repository snapshot  
**Output:** Clean development baseline and ownership agreement

1. Pull the current remote branch and compare it with the supplied ZIP; record newer commits and any work already merged.
2. Confirm each teammate's machine, OS, Python version, available hours, and preferred area; finalize owners and backup owners.
3. Run the existing router, memory, skills, and persona tests; log pass/fail and environment-specific missing dependencies.
4. Verify `python -m router smoke` against Token Factory in the authorized environment; confirm requested versus served models.
5. Validate Hermes/OpenShell sandbox startup, `agent/deploy.sh`, `agent/restore.sh`, and persistent volume behavior.
6. Inventory required API access: demo Gmail, Google Calendar, Telegram bot, Tavily, Nebius Token Factory; assign credential custodians.
7. Create `docs/decisions.md`, `docs/blockers.md`, `docs/integration-checklist.md`, and a single task board.
8. Agree on PR review, commit naming, merge ownership, daily integration checkpoint, and emergency rollback process.
9. Verify the official hackathon rules, exact submission deadline, video limit, sponsor/tool requirements, and team-size eligibility; do not infer these from the PDF.
10. Establish a known-good baseline commit/tag and write a one-command test/deploy quickstart.

**Definition of done:** Five developers can clone and test; at least two can deploy or reproduce the agent; all missing permissions and external services have named owners.

## PHASE 1 — DATA CONTRACTS, SCHEMAS, AND DATABASE DESIGN

**Priority:** P0 | **Lead:** Rishi | **Review:** Zayd + Vi | **Target:** Day 1  
**Depends on:** Phase 0  
**Output:** `docs/contracts.md`, typed contracts, database migrations

1. Define canonical `EmailRecord` fields: provider ID, thread ID, sender, subject, received timestamp, body excerpt, labels, raw-link reference, and ingestion metadata.-
2. Define `TriageResult`: bucket (`junk`, `actionable`, `due`, `newsletter`), reason, career flag, scam signals, extracted deadline, confidence, and model metadata.-
3. Define `Opportunity`: canonical URL, organization, title, source, discovery time, score, explanation, eligibility, deadline, and stable deduplication key.-
4. Define `Application`: opportunity reference, status (`found`, `applied`, `interview`, `decision`), status history, source email IDs, timestamps, and manual override metadata.-
5. Define `DeadlineEvent`, `DraftRecord`, `NotificationRecord`, `SecurityEvent`, and `CorrectionRecord` with stable identifiers.-
6. Specify UTC storage and timezone-aware display; represent unknown dates explicitly rather than inventing dates.-
7. Create SQLite schema and migration/bootstrap scripts for `data/keeper.db`; add unique indexes for email IDs, canonical opportunities, and provider event mappings.-
8. Specify allowed state transitions, precedence rules, deduplication semantics, and idempotency keys.-
9. Define adapter interfaces and error types for Gmail, Calendar, Telegram, Tavily, and model calls; create fakes with deterministic fixtures.-
10. Add schema-validation, migration, duplicate-processing, and state-transition tests; publish JSON examples for other developers.-

**Definition of done:** All five lanes can code against stable interfaces with no live API calls. Changes to contracts require review and migration notes.

## PHASE 2 — SECRETS, GOOGLE ACCESS, AND SERVICE ADAPTERS

**Priority:** P0 | **Lead:** Sid | **Review:** Vi | **Target:** Day 1–2  
**Depends on:** Phases 0–1  
**Output:** OAuth bootstrap and tested adapter layer

1. Create a disposable demo Google account and Google Cloud project; enable Gmail and Calendar APIs under appropriate test-user configuration. (Someone else do this, not me. -Rishi) 
2. Configure desktop OAuth with minimum scopes needed for reading/modifying Gmail, creating drafts, and editing calendar events; keep any demo seeding capability separate. -
3. Write a local interactive authorization script and a secure token installation script for the sandbox; do not commit generated credentials. -
4. Implement Gmail list/read/label/draft adapter functions with pagination, rate-limit handling, and explicit no-send behavior.-
5. Implement Calendar search/create/update adapter functions that store provider event IDs and support event updates.-
6. Configure Telegram bot/gateway and a minimal `sendMessage` adapter with retry and delivery logging.-
7. Implement Tavily search adapter with timeout, bounded results, error handling, and deterministic fake responses.-
8. Add adapter health checks and test all fakes before exercising real accounts.-
9. Verify the gateway user can read required configuration and perform authorized `/teach`/`/keep` writes without granting excessive permissions.-
10. Document service scopes, hostnames, sandbox secret locations, revocation steps, and known setup failure modes.-

**Definition of done:** Each service passes a standalone read/write-where-authorized smoke test; no real messages are sent; all secrets remain outside the repository.

---

# PART II — CAREER PIPELINE PRODUCT FEATURES

## PHASE 3 — LABELED DEMO INBOX AND EMAIL INGESTION

**Priority:** P0 | **Lead:** Zayd | **Review:** Sanjay | **Target:** Day 1–2  
**Depends on:** Phases 1–2  
**Output:** Seeded, repeatable, two-week demo inbox

1. Design roughly 30 synthetic emails spanning two weeks with balanced examples of junk, actionable, due, and newsletter categories. - 
2. Include internship offers, application confirmations, interview invitations, rejections, deadline reminders, extensions, hackathons, admissions/program notices, and routine newsletters. - 
3. Include ambiguous career emails to exercise the rule that career-related messages cannot be automatically classified as junk. - 
4. Include scam-pattern messages: upfront fees, suspicious sender-domain mismatches, unrealistic pay, and urgency language. - 
5. Include one clearly labeled prompt-injection email that attempts unauthorized exfiltration to a controlled, non-allowlisted test endpoint; never use real sensitive data. - 
6. Record gold labels, expected extracted deadlines, expected application transitions, scam flags, and scenario notes in versioned fixtures.-
7. Build a deterministic seeding script using the demo-only permitted mechanism; separate it from Keeper's production/runtime credentials.-
8. Build an ingestion service that stores Gmail message IDs, normalizes dates, skips already-seen messages, and records processing outcomes.-
9. Add tests for malformed email, missing subject, duplicate delivery, quoted replies, HTML/plaintext variants, and partial API failures.-
10. Provide a `reset-demo` procedure that restores the initial state without deleting developer credentials or unrelated data.-

**Definition of done:** A fresh sandbox can seed and ingest the same fixture set, producing stable IDs and expected counts without duplicates.

## PHASE 4 — NEMOTRON TRIAGE, CAREER PROTECTION, AND SCAM DETECTION

**Priority:** P0 | **Lead:** Zayd | **Review:** Rishi | **Target:** Day 2  
**Depends on:** Phases 1 and 3; existing router  
**Output:** `keeper triage run` core classifier

1. Implement a constrained Nano classification prompt returning validated structured fields and a one-sentence reason.-
2. Add robust parsing, schema checks, explicit `unknown`/review fallback, and retry behavior for malformed model outputs.-
3. Implement deterministic career keyword/sender checks **after** model classification; override any career-related junk decision to a reviewable bucket.-
4. Implement scam signal checks and distinguish `flagged` from `proven scam`; retain explanations and source evidence.-
5. Parse candidate dates and deadlines with timezone awareness; represent ambiguous dates as needing confirmation.-
6. Map triage results to safe Gmail labels and a persistent result table; avoid destructive archive/delete behavior.-
7. Preserve model request metadata and router logs; do not treat confidence scores as calibrated probabilities.-
8. Add fixture-based unit tests for all buckets, career override, scam signals, missing dates, and prompt-injection-like content.-
9. Add a dry-run command showing proposed changes without Gmail mutations, followed by explicit apply mode.-
10. Run the seeded inbox and compare actual versus expected labels; record all misses for the eval owner.-

**Definition of done:** Full inbox processes reliably; career mail is never sent to junk by Keeper; reasons and scam flags are visible; replay is idempotent.-

## PHASE 5 — APPLICATION TRACKER AND EMAIL-DRIVEN STATE MACHINE

**Priority:** P0 | **Lead:** Rishi | **Review:** Zayd | **Target:** Day 2–3  
**Depends on:** Phases 1, 3, 4  
**Output:** Persistent application lifecycle tracking

1. Create application repository operations and a transition history table with source evidence for every update.-
2. Resolve company/title identity from messages using normalized organization and opportunity keys; require review for ambiguous matches.-
3. Detect confirmation emails and move matching opportunities from `found` to `applied`.-
4. Detect interview invitations and move matching applications to `interview` without creating duplicate application records.-
5. Detect rejection/offer/decision signals and move matching applications to `decision`, preserving the specific outcome as metadata.-
6. Prevent backwards or contradictory transitions unless explicitly approved; retain full audit history.-
7. Expose CLI commands to list applications, inspect history, and apply a manual correction.-
8. Build a read-only `/apps` Telegram response backed by the same database, if gateway support permits.-
9. Test repeated email ingestion, out-of-order messages, multiple roles at one company, and ambiguous subject lines.-
10. Provide a deterministic scenario showing a single application moving through multiple states.-

**Definition of done:** A confirmation, interview invite, and rejection update one persistent application with an auditable history; reprocessing creates no duplicates.

## PHASE 6 — CALENDAR DEADLINES AND SAFE GMAIL DRAFTS

**Priority:** P0 | **Lead:** Vi (Calendar) + Zayd (Drafts) | **Review:** Rishi | **Target:** Day 3  
**Depends on:** Phases 2, 4, 5  
**Output:** Actionable inbox becomes calendar events and unsent drafts

1. Extract deadline candidates from relevant email with provenance and uncertainty flags.-
2. Validate dates and timezone assumptions; do not schedule ambiguous or past dates without an explicit review policy.-
3. Create a Calendar event for each confirmed deadline using an application/opportunity idempotency key.-
4. Store the provider event ID and update that event when an email explicitly extends a deadline.-
5. Test duplicate deadline emails, shortened deadlines, moved deadlines, and API retry scenarios.-
6. Draft context-aware replies with Super, including appropriate subject/thread context and concise professional tone.-
7. Create Gmail drafts through the draft API only; prohibit send operations in adapter interfaces and tests.-
8. Flag uncertain recruiter identity, missing context, and sensitive details for manual review rather than hallucinating them.-
9. Integrate event and draft links into the triage summary and application history.-
10. Demonstrate that a deadline extension moves the original event and that no draft has been sent.-

**Definition of done:** One email yields a safe unsent draft and a correct calendar event; an extension updates rather than duplicates the event.

## PHASE 7 — OPPORTUNITY RADAR, ELIGIBILITY, AND DEDUPLICATION

**Priority:** P0 | **Lead:** Vi | **Review:** Rishi | **Target:** Day 3–4  
**Depends on:** Phases 1–2; existing memory  
**Output:** `keeper radar run` with relevant opportunities

1. Define `profile.md` fields for target roles, graduation year, major, location constraints, work authorization where voluntarily supplied, and opportunity preferences.-
2. Build bounded Tavily queries from profile fields; prefer primary application URLs and credible sources.-
3. Normalize search results into opportunities with canonical URLs, title, organization, source, and discovery timestamp.-
4. Add heuristic filtering for obvious mismatches, closed/duplicate results, and unsupported eligibility claims.-
5. Use Nano to score relevance on a documented 1–10 rubric and generate one-line evidence-based explanations.-
6. Check eligibility against explicitly known profile attributes; label unknown conditions `unverified` rather than ineligible.-
7. Deduplicate by canonical URL plus organization/title; preserve prior opportunity history across daily runs.-
8. Persist top candidates and send a concise top-five Telegram digest with links, scores, and reasons.
9. Connect confidently verified deadlines to the calendar path; avoid treating search snippets as definitive closing dates.
10. Test empty results, API outage, duplicate URLs, irrelevant jobs, unknown authorization, and repeat runs.

**Definition of done:** A reproducible radar run produces relevant, deduplicated opportunities and a Telegram digest; every skip has a reason.

## PHASE 8 — TELEGRAM, SCHEDULING, AND CORRECTION LOOP

**Priority:** P0/P1 | **Lead:** Sid | **Review:** Rishi | **Target:** Day 3–4  
**Depends on:** Phases 2, 4–7; existing skills  
**Output:** Reliable background workflow and user control

1. Verify whether Hermes cron jobs can run the actual Keeper CLI commands in the deployed sandbox; document fallback if not.
2. Schedule triage approximately every 30 minutes and radar daily, with a manual trigger for demonstrations.
3. Add a run lock, durable last-run checkpoint, and failure log to prevent overlapping jobs and duplicate side effects.
4. Implement readable Telegram summaries: actionable count, due items, scam flags, new opportunities, and links to review.
5. Integrate `/apps` and expose simple health/status commands if supported by the existing gateway.
6. Test `/teach`, `/keep`, and `/drop` from the Telegram gateway user and fix permissions with least privilege.
7. Demonstrate a correction → proposed skill → explicit approval → subsequent changed decision; preserve before/after evidence.
8. Keep approval and deletion controls outside untrusted model-generated text; never allow email content to approve skills.
9. Test gateway restarts, Telegram delivery failure, cron overlap, stale sessions, and `/new` after persona updates.
10. Add manual CLI fallback paths so the demo remains possible when Telegram or cron is unavailable.

**Definition of done:** A scheduled or manually triggered run sends a useful digest, and the correction flow changes behavior only after user approval.

---

# PART III — TRUST, EVALUATION, AND JUDGE-FACING PROOF

## PHASE 9 — OPENSHELL NETWORK LOCKDOWN AND INJECTION DEFENSE

**Priority:** P0 | **Lead:** Sid | **Review:** Zayd | **Target:** Day 4–5  
**Depends on:** Working integrations from Phases 2–8  
**Output:** Verified least-privilege sandbox and controlled attack demo

1. Inventory actual egress hosts used by Token Factory, Google OAuth/API, Tavily, Telegram, and any necessary infrastructure.
2. Draft an OpenShell policy allowing only necessary destinations and methods, with documented justification for each exception.
3. Validate legitimate workflows before enabling final restrictions; record the exact policy version.
4. Enable the policy and rerun Gmail, Calendar, Telegram, Tavily, model calls, and scheduled commands.
5. Prepare a synthetic prompt-injection email that requests an unauthorized action toward a controlled, blocked test destination.
6. Keep test secrets synthetic and the target non-sensitive; avoid accidental transmission of credentials or real inbox content.
7. Verify both behavioral handling (the agent treats the email as data) and infrastructure enforcement (blocked network request), without assuming one proves the other.
8. Capture actual OpenShell deny/audit events and correlate them with the attack test timestamp.
9. Test policy failure modes: missing allowlisted host, overbroad allowlist, provider/model mismatch, and legitimate request rejection.
10. Freeze the known-good policy and keep rollback and recovery commands available for demo day.

**Definition of done:** Legitimate product workflows continue working, the controlled unauthorized request is denied, and the deny event can be shown from a real log.

## PHASE 10 — EVALUATION, MODEL COMPARISON, AND REPLAY

**Priority:** P0 | **Lead:** Sanjay | **Review:** Zayd | **Target:** Day 2–5, continuous  
**Depends on:** Phase 3 fixtures and feature interfaces  
**Output:** `eval/` runner and honest measurable results

1. Build a fixture loader that separates gold labels from model inputs and produces deterministic expected outcomes.
2. Evaluate classification by bucket with confusion matrix, per-class precision/recall, and overall accuracy.
3. Track career-to-junk errors separately as a high-priority safety metric; count unknown/manual-review outcomes explicitly.
4. Evaluate scam signal detection, deadline extraction, and tracker transitions with task-appropriate metrics.
5. Compare Nano, Super, and Ultra on the **same inputs** where available; log actual served model, latency, tokens, and estimated cost.
6. Treat model substitution or missing model access as a reportable limitation, not a valid comparison result.
7. Measure before/after correction behavior on a clearly identified case; avoid claiming general learning improvement from one example.
8. Build the two-week `without Keeper` versus `with Keeper` replay as an illustrative, fixture-driven scenario, with assumptions visible.
9. Add a single-command evaluation runner producing machine-readable JSON and a short Markdown summary.
10. Preserve failing examples and small regression fixtures so fixes can be verified without manually rerunning the entire inbox.

**Definition of done:** Metrics are reproducible and clearly labeled as demo-set results; charts use measured data, not fabricated performance.

## PHASE 11 — STATIC DASHBOARD AND DEMONSTRATION SURFACE

**Priority:** P0 | **Lead:** Sanjay | **Review:** Rishi | **Target:** Day 4–5  
**Depends on:** Phases 1, 5, 9, 10  
**Output:** `dashboard/` static HTML report

1. Design a single-page information hierarchy: overview, inbox triage, opportunities, application tracker, model costs, evaluation, and security evidence.
2. Write a Python report generator reading `keeper.db`, `router.db`, and `eval/results.json` through read-only access.
3. Render summary counts and application lifecycle history without requiring a server or live browser API calls.
4. Show model usage, latency, and cost with explicit indication that Hermes-native chat traffic may be absent from router logs.
5. Show classification results, career protection count, and honest sample sizes next to charts.
6. Present replay assumptions and `missed without Keeper` examples as scenario analysis rather than observed historical fact.
7. Present sanitized security audit evidence, including denied destination and timestamp; redact tokens and personal data.
8. Prefer self-contained assets for offline demo reliability; if using Chart.js CDN, supply a local fallback or pre-rendered charts.
9. Test generation with missing database tables, no events, failed evaluations, and partial results.
10. Freeze a static snapshot for backup and capture screenshots for README and submission.

**Definition of done:** Dashboard renders from real project artifacts, is legible during screen share, and still works without network access.

## PHASE 12 — FULL INTEGRATION, RELIABILITY, AND DEMO FREEZE

**Priority:** P0 | **Lead:** Rishi | **Review:** All | **Target:** Day 4–5  
**Depends on:** Phases 0–11  
**Output:** Release candidate and repeatable demo

1. Merge all P0 features behind stable contracts; resolve conflicting imports, environment variables, and command paths.
2. Extend `agent/bin/keeper` with `triage`, `radar`, `apps`, `eval`, and health commands while preserving existing commands.
3. Run the complete offline unit suite, fake-adapter integration tests, and authorized live smoke tests.
4. Execute a fresh-seed end-to-end flow from Gmail ingestion through Telegram, drafts, Calendar, tracker, and dashboard.
5. Re-run the same workflow and confirm no duplicate drafts, calendar events, opportunities, or application transitions.
6. Run a fault-injection pass: model timeout, Gmail 429, Tavily failure, Telegram outage, Calendar failure, and SQLite lock contention.
7. Verify the sandbox's persistent state and recovery after restart; test one teammate's clean-room setup instructions.
8. Run security and injection demonstration under the final network policy and capture proof artifacts.
9. Hold a P0 defect triage; fix only showstoppers and regressions after the feature freeze.
10. Tag the release candidate and record exact demo commands, fixture IDs, expected outputs, and fallback footage.

**Definition of done:** At least two teammates can reproduce the full demo from documented commands; P0 checks pass and no unreviewed changes enter the release candidate.

---

# PART IV — PRESENTATION, SUBMISSION, AND CONTINGENCY

## PHASE 13 — THREE-MINUTE DEMO STORY AND VIDEO

**Priority:** P0 | **Lead:** Sanjay (edit) + Sid (sandbox) + Rishi (story) | **Target:** Day 4–6  
**Depends on:** A working P0 path  
**Output:** Final and backup video, judge-ready narrative

1. Draft a one-sentence hook: student career opportunities get lost across email, job boards, calendars, and follow-ups.
2. Choose the central character and synthetic inbox scenario; remove unrelated side quests from the narrative.
3. Open with a controlled malicious email and the actual blocked request, clearly explaining the security boundary.
4. Show inbox before/after classification and why career-related messages remain visible.
5. Show one opportunity discovered by radar and its verified or clearly labeled tentative deadline.
6. Show a draft created but never sent, and a calendar event created or updated.
7. Show an application changing status from a seeded confirmation or interview email.
8. Show a correction and a subsequent behavior change, distinguishing a single-case demonstration from generalized learning.
9. End on the dashboard with measured accuracy, model costs, latency, and security audit evidence.
10. Record an early backup, then final capture; verify legibility, audio, length, link accessibility, and absence of secrets.

**Definition of done:** A viewer unfamiliar with Hermes or OpenShell understands the problem, product, security differentiation, and measured proof within the official time limit.

## PHASE 14 — README, ARCHITECTURE, FEEDBACK, AND FINAL SUBMISSION

**Priority:** P0 | **Lead:** Rishi + Sanjay | **Review:** All | **Target:** Day 5–6  
**Depends on:** Phase 12 release candidate  
**Output:** Complete submission package

1. Rewrite README with the product pitch, user workflow, feature status, stack, and precise sponsor-tool integration.
2. Include an architecture diagram or Mermaid chart: Gmail/Tavily → adapters → Keeper services → Nemotron router → SQLite/memory → Calendar/Telegram/dashboard, inside the security boundary where appropriate.
3. Document setup prerequisites, demo-only credentials, environment variables, test commands, and recovery instructions without exposing secrets.
4. Add screenshots, demo video link, and a concise reproducible walkthrough.
5. Document evaluation sample sizes, model comparisons, latency/cost calculation assumptions, and known limitations.
6. Update `NOTES_FEEDBACK.md` with actual friction, bugs, and observations about Nebius, NVIDIA, Hermes, and model behavior.
7. Audit Git history and release artifacts for secrets, personal data, accidental send permissions, and hardcoded tokens.
8. Confirm all teammates' contributions, team information, licenses, submission fields, and judging criteria.
9. Submit with a time buffer; independently open every public link in a private/incognito session where feasible.
10. Save the submission receipt, commit/tag, final video, dashboard snapshot, and a copy of the pitch in team storage.

**Definition of done:** Submission is accepted, every artifact opens, all claims are supported, and the final code matches the demonstrated release.

---

# PART V — SIX-DAY EXECUTION CALENDAR

| Day | Shared milestone | Sid | Rishi | Zayd | Vi | Sanjay |
|---|---|---|---|---|---|---|
| **1 — Oct 24** | Contracts, credentials, baseline | Sandbox/OAuth/adapters | Contracts/schema/integration | Fixture design + Gmail interface | Tavily/Calendar interface | Gold labels + eval skeleton |
| **2 — Oct 25** | Working inbox triage | Google/Telegram smoke | Tracker state machine | Triage + scam rules | Calendar adapter tests | Eval metrics + fixture completion |
| **3 — Oct 26** | Email → action loop | Cron + Telegram + skill permissions | Tracker/CLI integration | Drafts + triage stabilization | Calendar deadlines + radar | Model comparison + dashboard skeleton |
| **4 — Oct 27** | Full core workflow + backup video | Lockdown preparation | Full E2E integration | Inbox reliability | Radar/eligibility/dedupe | Replay + backup video + dashboard |
| **5 — Oct 28** | Security proof, release candidate | Final policy + deny evidence | Regression, recovery, freeze | Classification defects | Integration defects | Dashboard + final capture |
| **6 — Oct 29** | Submission only | Demo environment + fallback | README/release/submission | Last-mile smoke | Last-mile smoke | Final edit + links/screenshots |

**Daily cadence:** 15-minute kickoff; two focused build blocks; midday integration merge; evening 20-minute demo of actual behavior. Do not wait until Day 5 to integrate.

**Time allocation rule:** A developer blocked for >45 minutes posts a concise blocker and switches to a parallel task. One teammate must always protect the known-good branch.

---

# PART VI — DEPENDENCIES AND INTEGRATION GATES

```text
Phase 0 (baseline) ──> Phase 1 (contracts) ──> Phase 2 (adapters)
                         │                       │
                         ├──> Phase 3 (fixtures) ──> Phase 4 (triage)
                         │                              │
                         ├──> Phase 5 (tracker) <────────┤
                         │                              ├──> Phase 6 (drafts/calendar)
                         │                              └──> Phase 8 (Telegram/cron)
                         └──> Phase 7 (radar) <── Phase 2

Phases 3–8 ──> Phase 12 (integration) ──> Phase 13 (demo) ──> Phase 14 (submit)
      │                 ↑
      ├──> Phase 9 (security)
      ├──> Phase 10 (evaluation)
      └──> Phase 11 (dashboard)
```

**Gate A — End Day 1:** contracts merged, fake adapters agreed, credentials assigned, baseline tests run.  
**Gate B — End Day 2:** fixture inbox triaged with scores and safe labels; tracker logic works offline.  
**Gate C — End Day 3:** one email becomes a draft and event; tracker and Telegram work.  
**Gate D — End Day 4:** radar and end-to-end path work; backup video exists; P2 features frozen.  
**Gate E — End Day 5:** deny proof captured, eval/dashboard generated, clean demo replay passes.  
**Gate F — Day 6:** only bug fixes, video, documentation, and submission.

---

# PART VII — RISK REGISTER AND CUT PLAN

| Risk | Likelihood | Impact | Prevention / fallback |
|---|---|---|---|
| Google OAuth/API friction | High | High | Sid owns Day 1 bootstrap; offline fake adapters and seeded local fixtures |
| OpenShell egress policy blocks legitimate API | Medium | High | Inventory hosts, smoke first, apply policy after integration, retain rollback |
| Hermes gateway permissions break `/teach` | Medium | Medium | Test gateway UID early; show correction via terminal if necessary |
| Router silently serves Super for all requests | Medium | High for comparison | Log requested and served model; omit unsupported claims |
| Parallel development contract drift | High | High | Phase 1 schemas, PR ownership, integration checkpoints |
| Duplicate calendar/draft side effects | Medium | High | Idempotency keys and provider ID mapping; replay tests |
| Search results are stale or ineligible | High | Medium | Source links, unknown eligibility, dedupe, cautious deadline handling |
| Injection demo doesn't generate deny log | Medium | High | Prepare controlled reproducible test, collect proof early |
| Five-person merge conflict pileup | High | Medium | Small PRs, owner per directory, merge twice daily |
| Video/sandbox fails on submission day | Medium | Critical | Backup video Day 4, static dashboard snapshot, recovery scripts |

## Cut order — if behind schedule

1. **P2:** Google Doc cover letters.
2. **P2:** Backward-planned milestones.
3. **P2:** External application-page deadline verification.
4. **P1:** Rich interactive dashboard styling; keep a static evidence report.
5. **P1:** Telegram `/apps`; retain CLI/dashboard tracker.
6. **P1:** Three-model comparison; keep accurate Nano/Super measured results.
7. **P1:** Full replay animation; keep a transparent fixture-based summary.
8. **P1:** Telegram-native correction controls; show the already-built terminal `/teach`/`/keep` path.
9. **P1:** Elaborate eligibility scoring; retain simple, explainable filtering.

**Do not cut without a team-wide decision:** one working inbox triage, safe unsent draft, deadline event, tracker update, radar result, Telegram summary, real deny evidence, basic evaluation, demo recording, or final submission.

**If the complete pipeline is still broken at the Day 4 gate:** stop building features. Reduce the live demo to the shortest tested path and use honest offline fixtures for supplementary charts. Never fake logs, model comparisons, or security blocks.

---

# PART VIII — DEFINITION OF A WINNING RELEASE

### Engineering acceptance checklist

- [ ] Current repository baseline verified; all five developers know their lanes.
- [ ] Contracts merged and migrations tested.
- [ ] Demo account seeded with synthetic messages and gold labels.
- [ ] Triage correctly classifies, preserves career mail, and flags scam signals.
- [ ] Drafts are created but never sent; no send permission is requested.
- [ ] Deadline extension updates an existing Calendar event.
- [ ] Application status changes are evidence-backed and idempotent.
- [ ] Radar returns relevant, deduplicated, sourced opportunities.
- [ ] Telegram sends a useful digest and manual fallback exists.
- [ ] Correction requires approval and changes a subsequent test case.
- [ ] OpenShell blocks the controlled unauthorized egress attempt with an actual audit record.
- [ ] Evaluation reports sample sizes, limitations, accuracy, latency, and cost honestly.
- [ ] Dashboard is generated from real stored outputs.
- [ ] A teammate other than the primary infrastructure owner can reproduce the demo.
- [ ] Backup video, final video, README, feedback, and submission links are verified.

### Final demonstration sequence

**0:00–0:25 — Hook:** Career email is fragmented; malicious instructions are a risk.  
**0:25–0:55 — Security:** Show attack attempt and actual deny log.  
**0:55–1:25 — Inbox:** Triage with protected career mail and scam warning.  
**1:25–1:55 — Action:** Draft, deadline event, and application status update.  
**1:55–2:20 — Discovery:** Radar result and Telegram digest.  
**2:20–2:40 — Learning:** Approved correction and changed follow-up behavior.  
**2:40–3:00 — Proof:** Evaluation, model efficiency, and final product statement.

*Adjust timing to official hackathon rules and actual reliable footage. A smooth two-minute demonstration beats a broken three-minute one.*

---

## APPENDIX A — PROPOSED REPOSITORY TARGET STRUCTURE

```text
keeper/
├── agent/                  # Existing Hermes/OpenShell deployment and policies
├── router/                 # Existing Nemotron tier routing and call logging
├── memory/                 # Existing persistent Markdown memory
├── skills/                 # Existing correction-to-skill approval flow
├── keeper_core/            # NEW schemas, state machine, workflow orchestration
├── integrations/           # NEW gmail.py, calendar.py, telegram.py, tavily.py + fakes
├── triage/                 # NEW classification, career guard, scam rules, dates
├── radar/                  # NEW queries, scoring, eligibility, deduplication
├── data/                   # Runtime SQLite/fixtures; never commit secrets
├── eval/                   # NEW labeled fixtures, metrics, replay, result JSON
├── dashboard/              # Static report generator and output
├── demo/                   # Synthetic inbox, seeding, scripts, video plan
├── docs/                   # Contracts, architecture, decisions, runbook
├── tests/                  # Unit and integration tests
├── scripts/                # Existing probes + setup/smoke tools
└── README.md
```

The new directory names are **proposals**, not claims about existing files. If the team prefers a smaller structure, preserve module boundaries and imports rather than creating folders for their own sake.

## APPENDIX B — SOURCE AND ASSUMPTION NOTES

- **Source:** User-provided `Keeper build plan.pdf` and `nebius-x-nvidia-claude-dreamy-tesla-7tv0ko.zip`, inspected October 24, 2026.
- **Existing implementation:** Router, memory, skills, sandbox deployment, and unit tests are present in the uploaded repository; no claim is made that all tests pass in the team's current environment.
- **Existing product scope:** Inbox triage, radar, scam detection, drafts, Calendar, tracker, Telegram, learning, security, evaluation, replay, and dashboard come from the original plan.
- **New planning recommendations:** Five-person lane allocation, integration gates, proposed directories, idempotency safeguards, fallback paths, and the expanded phase sequence are recommendations for this team.
- **Unverified:** Official submission rules, actual latest remote repository state, current service credentials, teammate specialties, and live deployment status. Confirm before executing.
