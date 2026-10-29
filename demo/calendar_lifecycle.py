"""Offline, repeatable Calendar deadline scenario. Run with -m demo.calendar_lifecycle."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.application_activity import activity_for_application
from keeper_core.applications import create_application
from keeper_core.contracts import (
    EmailRecord, Eligibility, IngestionMetadata, Opportunity, TriageBucket, TriageResult,
)
from keeper_core.deadlines import extract_deadline_candidates
from keeper_core.db import migrate
from keeper_core.fakes import FakeCalendar
from keeper_core.calendar_deadlines import sync_deadline_candidate
from keeper_core.ingest import insert_email_once, insert_opportunity_once
from keeper_core.time import from_storage
from keeper_core.workflow import canonical_url, opportunity_key


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
ORGANIZATION = "Example Inc"
TITLE = "Software Engineer Intern"
URL = canonical_url("https://example.test/careers/software-engineer-intern")
EMAILS = (
    ("deadline", "Application deadline", "Applications are due by", "2026-11-20T17:00:00Z"),
    ("extension", "Deadline extended", "The application deadline has been extended to", "2026-12-15T17:00:00Z"),
)


def run_scenario() -> dict:
    """Use synthetic emails, a fake Calendar, and a temporary product database."""
    with tempfile.TemporaryDirectory(prefix="keeper-calendar-scenario-") as directory:
        path = Path(directory) / "keeper.db"
        migrate(path)
        calendar = FakeCalendar()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            opportunity = Opportunity(
                "scenario:opportunity", opportunity_key(URL, ORGANIZATION, TITLE), URL,
                ORGANIZATION, TITLE, "scenario", NOW, None, None,
                Eligibility.UNVERIFIED, None, None,
            )
            insert_opportunity_once(conn, opportunity)
            conn.commit()
            application = create_application(
                conn, opportunity.id, created_at=NOW, source_evidence=URL,
            )

        updates = []
        for index, (name, subject, phrase, timestamp) in enumerate(EMAILS, 1):
            received = NOW + timedelta(hours=index)
            body = f"{ORGANIZATION}, {TITLE}: {phrase} {timestamp}."
            email_id = f"scenario:email:{name}"
            email = EmailRecord(
                email_id, "fixture", name, None, "recruiting@example.test", subject,
                received, body, (), None,
                IngestionMetadata(received, "scenario:run", "offline-synthetic"),
            )
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                insert_email_once(conn, email)
                conn.commit()
            triage = TriageResult(
                f"scenario:triage:{name}", email.id, TriageBucket.DUE,
                "Synthetic career deadline fixture", True, (), from_storage(timestamp),
                None, None, received, deadline_evidence=body,
            )
            candidates = extract_deadline_candidates(email, triage, now=received)
            if len(candidates) != 1:
                raise AssertionError("fixture should contain one source-backed deadline")
            result = sync_deadline_candidate(
                path, calendar, candidates[0], opportunity_id=opportunity.id,
                application_id=application.id, now=received,
            )
            replay = sync_deadline_candidate(
                path, calendar, candidates[0], opportunity_id=opportunity.id,
                application_id=application.id, now=received,
            )
            updates.append({"source_email_id": email.id, "result": result.status,
                            "replay": replay.status, "provider_event_id": result.provider_event_id})

        with closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(
                "SELECT id, deadline_at, evidence, provider_event_id FROM deadline_events"
            ).fetchall()
            activities = activity_for_application(conn, application.id)
        if len(rows) != 1 or len(calendar.events_by_key) != 1:
            raise AssertionError("scenario should persist one Calendar event")
        row = rows[0]
        if [item["kind"] for item in activities] != ["calendar_created", "calendar_updated"]:
            raise AssertionError("scenario should persist creation and update activity")
        return {
            "mode": "offline-synthetic",
            "application_id": application.id,
            "updates": updates,
            "event": {"id": row[0], "provider_event_id": row[3], "deadline_at": row[1],
                      "source_email_id": activities[-1]["source_email_id"],
                      "source_evidence": row[2]},
            "application_activity": [
                {"kind": item["kind"], "source_email_id": item["source_email_id"],
                 "source_evidence": item["source_evidence"], "provider_id": item["provider_id"]}
                for item in activities
            ],
        }


def main() -> int:
    print(json.dumps(run_scenario(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
