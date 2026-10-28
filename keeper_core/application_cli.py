"""Read application state and apply explicit, auditable user corrections."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .applications import apply_manual_correction, get_application, list_applications
from .application_activity import activity_for_application
from .contracts import ApplicationStatus
from .db import default_db_path, migrate
from .time import to_storage
from .workflow import TransitionConflict


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List applications, inspect history, or apply a user correction")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="Keeper SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list tracked applications")
    sub.add_parser("telegram", help="read-only plain-text summary for the /apps gateway command")
    history = sub.add_parser("history", help="show all evidence and transitions for one application")
    history.add_argument("application_id")
    correct = sub.add_parser("correct", help="apply an explicitly approved manual correction")
    correct.add_argument("application_id")
    correct.add_argument("--status", required=True, choices=[status.value for status in ApplicationStatus])
    correct.add_argument("--outcome", help="specific decision outcome, required for decision status")
    correct.add_argument("--actor", required=True, help="reference identifying the approving user")
    correct.add_argument("--reason", required=True, help="why the status should change")
    correct.add_argument("--evidence", required=True, help="user-reviewed source or supporting detail")
    correct.add_argument("--correction-id", required=True, help="stable ID to use for safe retries")
    correct.add_argument("--approve", action="store_true", help="explicitly approve and apply this correction")
    return parser


def _read_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("application database does not exist")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _summary(conn: sqlite3.Connection, app) -> dict:
    organization, title = conn.execute(
        "SELECT organization, title FROM opportunities WHERE id = ?", (app.opportunity_id,),
    ).fetchone()
    return {
        "id": app.id, "opportunity_id": app.opportunity_id,
        "organization": organization, "title": title,
        "status": app.status.value, "decision_outcome": app.decision_outcome,
        "updated_at": to_storage(app.updated_at),
    }


def _history(conn: sqlite3.Connection, app) -> dict:
    result = _summary(conn, app)
    result["created_at"] = to_storage(app.created_at)
    result["source_email_ids"] = list(app.source_email_ids)
    result["manual_override"] = (
        {"actor_reference": app.manual_override.actor_reference,
         "reason": app.manual_override.reason,
         "applied_at": to_storage(app.manual_override.applied_at)}
        if app.manual_override else None
    )
    result["history"] = [
        {"id": change.id, "status": change.status.value,
         "changed_at": to_storage(change.changed_at),
         "source_email_id": change.source_email_id,
         "reason": change.reason, "source_evidence": change.source_evidence,
         "manual": change.manual, "correction_id": change.correction_id,
         "decision_outcome": change.decision_outcome}
        for change in app.status_history
    ]
    result["activity"] = activity_for_application(conn, app.id)
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "telegram":
        from .application_telegram import format_apps_response
        try:
            with closing(_read_connection(args.db)) as conn:
                print(format_apps_response(conn))
        except (ValueError, sqlite3.Error):
            print("Application tracker is not ready.")
        return 0
    try:
        if args.command == "correct":
            if not args.approve:
                raise ValueError("manual correction requires --approve")
            if (args.status == "decision") != bool(args.outcome and args.outcome.strip()):
                raise ValueError("outcome is required only for decision status")
            if not args.db.is_file():
                raise ValueError("application database does not exist")
            migrate(args.db)
            with closing(sqlite3.connect(args.db)) as conn, conn:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("BEGIN IMMEDIATE")
                app = apply_manual_correction(
                    conn, args.application_id, ApplicationStatus(args.status),
                    correction_id=args.correction_id, actor_reference=args.actor,
                    reason=args.reason, source_evidence=args.evidence,
                    approved_at=datetime.now(timezone.utc), decision_outcome=args.outcome,
                )
                result = {**_summary(conn, app), "correction_id": args.correction_id}
        else:
            with closing(_read_connection(args.db)) as conn:
                if args.command == "list":
                    result = [_summary(conn, app) for app in list_applications(conn)]
                else:
                    app = get_application(conn, args.application_id)
                    if app is None:
                        raise ValueError("application does not exist")
                    result = _history(conn, app)
    except (ValueError, sqlite3.Error, TransitionConflict) as exc:
        print(f"Applications: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
