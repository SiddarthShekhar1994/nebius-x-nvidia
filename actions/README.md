# actions: what Keeper does after triage

`keeper act` reads the triage decisions saved in keeper.db and offers each one to three steps, in this order. Each step is a thin wrapper: the logic lives in `keeper_core` (Rishi's tracker, deadline and draft code), and this package only decides which emails go where and reports what happened.

| Step | Wrapper | Logic | Picks | Does |
| --- | --- | --- | --- | --- |
| tracker | `tracker.py` | `keeper_core.application_messages` | career mail, actionable or due, no scam warning | moves a tracked application on confirmation, interview and decision emails |
| deadlines | `deadlines.py` | `keeper_core.deadlines`, `keeper_core.calendar_deadlines` | due, with a verified deadline, no scam warning | one calendar event per tracked opportunity; an extension moves it |
| drafts | `drafts.py` | `keeper_core.reply_drafts` | actionable Gmail mail, no scam warning | one unsent Gmail draft, written by Super; never sent |

The tracker runs first because it links an email to its application, and the other two use that link.

## What it won't do on its own

Keeper is deliberately conservative, so a step often answers `review` instead of acting:

- **The email must name a tracked opportunity.** The tracker and the calendar only act when the email names both the organization and the role of one opportunity Keeper already tracks (from the radar, or added by hand). Mail about anything else is `review`.
- **Deadlines must be explicit and in the future.** A past date, more than one date, or a date without a cue word such as "deadline", "apply by" or "applications close" is `review` or `skipped`.
- **Drafts without context are holding replies.** With no linked application, an unverified sender or a sensitive request, the draft asks a short clarifying question and is flagged for review.

## Statuses

`done`, `already_done` (an earlier run did it), `planned` (dry run), `skipped` (nothing to do), `review` (needs a person, with the reason), `failed` (a provider error, by class name only).

## Running it

    python -m triage run --save --db data/keeper.db                     # save decisions first
    python -m actions run --fake --db data/keeper.db                    # dry run, offline fakes
    python -m actions run --gmail --account DEMO --token TOKEN --db data/keeper.db            # dry run, real account
    python -m actions run --gmail --account DEMO --token TOKEN --db data/keeper.db --apply    # act

In the sandbox the command is `keeper act run ...`. A dry run never writes to keeper.db or calls a provider. `--gmail` uses the same private runtime token as `triage run --gmail` and refuses a database that belongs to another account. `--fake --apply` refuses a database with Gmail decisions, so offline draft and event IDs never end up next to real ones. Rejected Google or Nebius credentials stop the run with status 2.

Tests: `python -m unittest tests.test_actions`.
