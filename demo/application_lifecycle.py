"""Offline, repeatable application lifecycle scenario. Run with -m demo.application_lifecycle."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.application_messages import process_application_email
from keeper_core.applications import create_application, get_application, list_applications
from keeper_core.contracts import EmailRecord, Eligibility, IngestionMetadata, Opportunity
from keeper_core.db import migrate
from keeper_core.ingest import insert_email_once, insert_opportunity_once
from keeper_core.time import to_storage
from keeper_core.workflow import canonical_url, opportunity_key


START = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
ORGANIZATION = "Example Inc"
TITLE = "Software Engineer Intern"
URL = canonical_url("https://example.test/careers/software-engineer-intern")
MESSAGES = (
    ("Application received", "Example Inc received your application for Software Engineer Intern."),
    ("Interview invitation", "Example Inc invites you to interview for Software Engineer Intern."),
    ("Application decision", "Example Inc: unfortunately, we will not be moving forward with your application for Software Engineer Intern."),
)


def run_scenario() -> dict:
    """Use a temporary product database and return a stable, source-backed trace."""
    with tempfile.TemporaryDirectory(prefix="keeper-application-scenario-") as directory:
        path = Path(directory) / "keeper.db"
        migrate(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            opportunity = Opportunity(
                "scenario:opportunity", opportunity_key(URL, ORGANIZATION, TITLE), URL,
                ORGANIZATION, TITLE, "scenario", START, None, None,
                Eligibility.UNVERIFIED, None, None,
            )
            insert_opportunity_once(conn, opportunity)
            app = create_application(conn, opportunity.id, created_at=START, source_evidence=URL)
            updates = []
            for number, (subject, body) in enumerate(MESSAGES, 1):
                at = START + timedelta(hours=number)
                email = EmailRecord(
                    f"scenario:email:{number}", "fixture", str(number), None,
                    "recruiting@example.test", subject, at, body, (), None,
                    IngestionMetadata(at, "scenario:run", "fixture"),
                )
                insert_email_once(conn, email)
                result = process_application_email(conn, email)
                updates.append({"email_id": email.id, "result": result.status,
                                "status": result.application.status.value if result.application else None})
                replay = process_application_email(conn, email)
                if result.status != "updated" or replay.status != "reused":
                    raise AssertionError("scenario transition or replay failed")
            final = get_application(conn, app.id)
            if len(list_applications(conn)) != 1 or final.decision_outcome != "rejected":
                raise AssertionError("scenario did not produce one rejected application")
            return {
                "application_id": app.id,
                "opportunity_id": opportunity.id,
                "updates": updates,
                "final_status": final.status.value,
                "decision_outcome": final.decision_outcome,
                "history": [
                    {"status": change.status.value,
                     "changed_at": to_storage(change.changed_at),
                     "source_email_id": change.source_email_id,
                     "source_evidence": change.source_evidence,
                     "decision_outcome": change.decision_outcome}
                    for change in final.status_history
                ],
            }


def main() -> int:
    print(json.dumps(run_scenario(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
