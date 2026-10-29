# Demo story: one internship, from application to an extended deadline

The 75-email inbox shows sorting and scam warnings, but none of its emails names an internship Keeper tracks, and all its deadlines are past. So `keeper act` mostly answers "review". This story fills that gap with five more synthetic emails about one tracked internship. It's the same internship the radar demo finds: Example Inc, Software Engineer Intern.

| Part | Email | What Keeper does |
| --- | --- | --- |
| 1 | Application received | Tracker: found → applied |
| 1 | Interview invitation | Tracker: applied → interview, plus an unsent reply draft |
| 1 | Take-home due 2026-11-06 17:00 UTC | One calendar event |
| 2 | Deadline extended to 2026-11-13 17:00 UTC | Moves that same event; no second one |
| 2 | Fake recruiter: "pay the $75 processing fee today" | Scam warning; no tracker change, event or draft |

The emails are in `demo/fixtures/story_part1.json` and `story_part2.json`. Each body is short enough to fit in Gmail's preview, which is what the tracker and the deadline step read. Each one names both "Example Inc" and "Software Engineer Intern", the identity the tracker needs before it changes anything. `tests/test_demo_story.py` checks the wording against Keeper's rules and plays both parts offline.

## Running it

Run this in Ubuntu (WSL) from the repo root. First load the 75-email inbox (`demo/SEED_AND_RESET.md`), then run `agent/openshell/setup-google.sh` and `agent/deploy.sh`. Then:

```bash
bash agent/demo-story.sh track    # once: Keeper starts tracking the internship
bash agent/demo-story.sh part1    # three emails arrive; tracker, calendar and drafts react
bash agent/demo-story.sh part2    # the deadline moves; the fake recruiter is flagged
```

Each part does four things:
1. Adds its emails to the demo inbox with the seeding token (`~/keeper-secrets/seed.json`).
2. Copies their manifest into the sandbox.
3. Sorts and labels them with Nemotron.
4. Runs `keeper act --apply`.

It then prints, for each email, what triage decided and what each step did, followed by the tracker summary that `/apps` shows in Telegram. Rerunning a part repeats nothing.

`track` records the internship as found on 2026-10-26. That date matters: the tracker never lets an email older than an application's last change move it, so an internship first tracked today would ignore these October emails.

## On camera

1. **Gmail:** the inbox before part 1. The story emails are the newest.
2. **Part 1:** the terminal shows the tracker moving to interview and the calendar event created. In Gmail, the KEEPER_ labels and the unsent reply draft. In Google Calendar, the event on Nov 6.
3. **Part 2:** the same event now on Nov 13. The fake recruiter's email has a `KEEPER_SCAM_REVIEW` label and no draft.
4. **Telegram,** if set up: `/apps` shows the application at interview.
