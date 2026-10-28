"""keeper act: run the tracker, deadline and draft steps over saved triage results.

    python -m actions run --fake                 # dry run with offline Gmail and Calendar
    python -m actions run --fake --apply         # let the steps act (still offline)
"""
import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from keeper_core.adapters import AdapterError
from keeper_core.db import default_db_path
from keeper_core.fakes import FakeCalendar, FakeGmail
from triage.store import saved_decisions

from . import deadlines, drafts, tracker
from .base import FAILED, ActionContext, Outcome

# Tracker first, so deadlines and drafts can link to the application it creates.
STEPS = (tracker, deadlines, drafts)


def act(pairs, ctx, steps=STEPS):
    """Offer every saved decision to every step; a provider error fails that one item, not the run."""
    outcomes = []
    for email, result in pairs:
        for step in steps:
            if not step.wants(email, result):
                continue
            try:
                outcome = step.run(email, result, ctx)
            except AdapterError as exc:
                outcome = Outcome(step.NAME, email.id, FAILED, type(exc).__name__)
            outcomes.append(outcome)
    return outcomes


def summarize(outcomes, steps=STEPS):
    counts = {step.NAME: Counter() for step in steps}
    for outcome in outcomes:
        counts[outcome.step][outcome.status] += 1
    return {name: dict(sorted(counter.items())) for name, counter in counts.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="keeper act", description="Act on saved triage results.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="tracker, deadlines and drafts for every saved decision")
    run.add_argument("--db", type=Path, default=default_db_path())
    run.add_argument("--apply", action="store_true", help="let the steps act; the default is a dry run")
    run.add_argument("--fake", action="store_true", help="offline Gmail and Calendar fakes")
    args = parser.parse_args(argv)
    if not args.fake:
        parser.error("live Gmail and Calendar wiring is not built yet; use --fake")

    pairs = saved_decisions(args.db)
    ctx = ActionContext(
        db_path=args.db, gmail=FakeGmail(tuple(email for email, _ in pairs)), calendar=FakeCalendar(),
        model=None, now=lambda: datetime.now(timezone.utc), dry_run=not args.apply,
    )
    outcomes = act(pairs, ctx)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run", "adapters": "fake", "decisions": len(pairs),
        "summary": summarize(outcomes), "outcomes": [asdict(outcome) for outcome in outcomes],
    }, indent=2))
    return 1 if any(outcome.status == FAILED for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
