# actions: what Keeper does after triage

`keeper act` reads the triage decisions saved in keeper.db and offers each one to three steps, in this order:

| Step | Module | Owner | Picks (starting rule in `wants`) | Does |
| --- | --- | --- | --- | --- |
| tracker | `tracker.py` | Rishi | career mail, actionable or due, no scam warning | moves an application forward on confirmation, interview and rejection emails |
| deadlines | `deadlines.py` | Vi | due, with a verified deadline, no scam warning | one calendar event per deadline; an extension moves the existing event |
| drafts | `drafts.py` | Zayd | actionable, no scam warning | one Gmail draft per email, written by Super; never sent |

Each module ships as a working stub that returns `not_built`, so the command runs end to end from day one. An owner replaces `run()` and narrows `wants()` if needed. The tracker's starting rule picks 34 of the 75 fixture emails; it should end up acting only on confirmations, interviews and rejections, which takes the email text, not just the triage bucket.

## The interface (`base.py`)

    NAME: str
    wants(email, result) -> bool
    run(email, result, ctx) -> Outcome

- `email` is an `EmailRecord`, `result` its saved `TriageResult`.
- `ctx` is an `ActionContext`: `db_path`, `gmail`, `calendar`, `model`, `now()` (UTC) and `dry_run`.
- `Outcome(step, email_id, status, detail, provider_id=None)`. Status is one of `done`, `already_done`, `planned`, `skipped`, `failed`, `not_built`. Keep `detail` to one line with no email body and no secrets.

Rules every step follows:

1. **Idempotent.** Running `keeper act` twice creates nothing new the second time. Build IDs and provider idempotency keys with `keeper_core.workflow.stable_key`, and return `already_done` when the work exists.
2. **Dry run touches nothing.** When `ctx.dry_run` is true, no provider calls and no database writes; return `planned` with what would happen.
3. **Provider errors are fine to raise.** `act()` turns an `AdapterError` into a `failed` outcome for that one email and carries on. Anything else is a bug and stops the run.
4. **Never send email.** Drafts only. There is no send call in any adapter, and there must not be one here.
5. **Skip scams.** Every starting rule already excludes results with scam warnings; keep it that way.

## Running it

    python -m triage run --save --db data/keeper.db      # save decisions first (offline here)
    python -m actions run --fake --db data/keeper.db     # dry run with offline Gmail and Calendar
    python -m actions run --fake --apply --db data/keeper.db

In the sandbox the same command is `keeper act run ...`. Live Gmail and Calendar wiring (the token and account options, like `triage run --gmail`) is the last piece of the `keeper act` command and is Rishi's; until then the command refuses to run without `--fake`.

Tests: `python -m unittest tests.test_actions`. Add your step's tests next to them, using `keeper_core.fakes` and a temporary database.
