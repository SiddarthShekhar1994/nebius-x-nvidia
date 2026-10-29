"""keeper act: run the tracker, deadline and draft steps over saved triage results.

    python -m actions run --fake                         # dry run, offline Gmail and Calendar
    python -m actions run --gmail --account A --token T  # dry run against the real demo account
    python -m actions run --gmail --account A --token T --apply
"""
import argparse
import json
import sqlite3
import sys
import uuid
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from keeper_core.adapters import AdapterError, AuthenticationError, ModelAnswer
from keeper_core.contracts import ModelMetadata
from keeper_core.db import default_db_path, migrate
from keeper_core.fakes import FakeCalendar, FakeGmail, FakeModel
from triage.store import saved_decisions

from . import deadlines, drafts, tracker
from .base import FAILED, REVIEW, ActionContext, Outcome

# Tracker first: it links an email to its application, which deadlines and drafts then use.
STEPS = (tracker, deadlines, drafts)
OFFLINE_DRAFT = "Thank you for the update. I will review the details and reply shortly."


def act(pairs, ctx, steps=STEPS):
    """Offer every saved decision to every step.

    A provider error or a guard in keeper_core fails or flags that one item and the
    run carries on; rejected credentials stop the whole run.
    """
    outcomes = []
    for email, result in pairs:
        for step in steps:
            if not step.wants(email, result):
                continue
            try:
                outcome = step.run(email, result, ctx)
            except AuthenticationError:
                raise
            except AdapterError as exc:
                outcome = Outcome(step.NAME, email.id, FAILED, type(exc).__name__)
            except ValueError as exc:  # keeper_core's guards: evidence or identity needs a person
                outcome = Outcome(step.NAME, email.id, REVIEW, str(exc))
            outcomes.append(outcome)
    return outcomes


def summarize(outcomes, steps=STEPS):
    counts = {step.NAME: Counter() for step in steps}
    for outcome in outcomes:
        counts[outcome.step][outcome.status] += 1
    return {name: dict(sorted(counter.items())) for name, counter in counts.items()}


def _bound_account(db_path):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'triage_account'").fetchone():
            return None
        row = conn.execute("SELECT account FROM triage_account WHERE singleton = 1").fetchone()
    return row[0] if row else None


def _adapters(args, pairs):
    if args.fake:
        model = FakeModel({"draft": ModelAnswer(OFFLINE_DRAFT, ModelMetadata("offline", "offline", "offline"))})
        return FakeGmail(tuple(email for email, _ in pairs)), FakeCalendar(), model
    from integrations.calendar import LiveCalendar
    from integrations.draft_model import DraftRouterModel
    from router.config import load_settings
    from triage.credentials import gmail_from_token
    gmail = gmail_from_token(args.token, run_id="act:" + uuid.uuid4().hex)
    return gmail, LiveCalendar.from_token_file(args.token, args.db), DraftRouterModel(load_settings())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="keeper act", description="Act on saved triage results.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="tracker, deadlines and drafts for every saved decision")
    run.add_argument("--db", type=Path, default=default_db_path())
    run.add_argument("--apply", action="store_true", help="act; the default is a dry run that changes nothing")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--fake", action="store_true", help="offline Gmail, Calendar and draft model")
    source.add_argument("--gmail", action="store_true", help="the demo account's Gmail and Calendar, Super for drafts")
    run.add_argument("--account", help="disposable demo Gmail account the database belongs to")
    run.add_argument("--token", type=Path, help="private runtime OAuth token outside the checkout")
    args = parser.parse_args(argv)
    if args.gmail and not (args.account and args.token):
        parser.error("--gmail requires --account and --token")
    if args.fake and (args.account or args.token):
        parser.error("--account and --token are only for --gmail")

    pairs = saved_decisions(args.db)
    try:
        if args.gmail and pairs and _bound_account(args.db) != args.account.casefold():
            raise ValueError("the database's triage decisions belong to another Gmail account")
        if args.fake and args.apply and any(email.provider == "gmail" for email, _ in pairs):
            raise ValueError("--fake --apply would record offline drafts and events in a Gmail database")
        if args.apply and args.db.is_file():
            migrate(args.db)
        gmail, calendar, model = _adapters(args, pairs)
        ctx = ActionContext(db_path=args.db, gmail=gmail, calendar=calendar, model=model,
                            now=lambda: datetime.now(timezone.utc), dry_run=not args.apply)
        outcomes = act(pairs, ctx)
    except AuthenticationError:
        print("Google or Nebius credentials missing or rejected; nothing further was changed.", file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"keeper act: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run", "adapters": "fake" if args.fake else "gmail",
        "decisions": len(pairs), "summary": summarize(outcomes),
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }, indent=2))
    return 1 if any(outcome.status == FAILED for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
