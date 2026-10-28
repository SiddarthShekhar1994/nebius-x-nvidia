import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from integrations.calendar import LiveCalendar
from keeper_core.calendar_deadlines import sync_deadline_candidate
from keeper_core.contracts import EmailRecord, IngestionMetadata, TriageBucket, TriageResult
from keeper_core.db import migrate
from keeper_core.deadlines import DeadlineReviewPolicy, extract_deadline_candidates
from keeper_core.ingest import insert_email_once
from keeper_core.time import to_storage
from tests.test_calendar_live import Service, error


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class CalendarDeadlineSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keeper.db"
        migrate(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """INSERT INTO opportunities
                   (id, dedupe_key, canonical_url, organization, title, source,
                    discovered_at, eligibility)
                   VALUES ('opp:1', 'key:1', 'https://example.test/job/1',
                           'Example Inc', 'Software Engineer Intern', 'fixture', ?, 'unverified')""",
                (to_storage(NOW),),
            )
            conn.execute(
                """INSERT INTO applications(id, opportunity_id, status, created_at, updated_at)
                   VALUES ('app:1', 'opp:1', 'found', ?, ?)""",
                (to_storage(NOW), to_storage(NOW)),
            )
        self.service = Service()
        self.waits = []
        self.calendar = LiveCalendar(self.service, self.path, now=lambda: NOW,
                                     sleep=self.waits.append)

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, number, body, *, received_at=None):
        at = received_at or NOW + timedelta(minutes=number)
        email = EmailRecord(
            f"gmail:{number}", "gmail", str(number), None,
            "recruiter@example.test", "Application deadline", at, body, (),
            f"gmail:{number}", IngestionMetadata(at, "run", "fixture"),
        )
        triage = TriageResult(f"triage:{number}", email.id, TriageBucket.DUE,
                              "Career deadline", True, (), None, None, None, NOW)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            insert_email_once(conn, email)
        return extract_deadline_candidates(email, triage, now=NOW)[0]

    def row(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(
                """SELECT id, application_id, source_email_id, deadline_at, evidence,
                          provider, provider_event_id FROM deadline_events"""
            ).fetchone()

    def sync(self, candidate, *, policy=None):
        return sync_deadline_candidate(self.path, self.calendar, candidate,
                                       opportunity_id="opp:1", application_id="app:1",
                                       now=NOW, policy=policy)

    def test_create_and_duplicate_emails_keep_one_provider_event(self):
        first = self.candidate(1, "Apply by 2026-11-02T09:30:00Z.")
        result = self.sync(first)
        self.assertEqual(result.status, "created")
        original = self.row()
        self.assertEqual(original[1], "app:1")
        self.assertEqual(original[2], "gmail:1")
        self.assertEqual(original[3], "2026-11-02T09:30:00.000000Z")
        self.assertEqual(original[5], "google-calendar")
        self.assertEqual(original[6], result.provider_event_id)
        self.assertEqual(self.sync(first).status, "unchanged")
        duplicate = self.candidate(2, "Apply by 2026-11-02T09:30:00Z.")
        self.assertEqual(self.sync(duplicate).status, "unchanged")
        self.assertEqual(len(self.service.items), 1)
        self.assertEqual(len([name for name, _ in self.service.calls if name == "insert"]), 1)

    def test_explicit_extension_and_moved_date_patch_original_event(self):
        first = self.candidate(1, "Apply by 2026-11-02T09:30:00Z.")
        self.sync(first)
        original_id = self.row()[6]
        extension = self.candidate(2, "Application deadline extended to 2026-11-05T09:30:00Z.")
        result = self.sync(extension)
        self.assertEqual(result.status, "updated")
        self.assertEqual(self.row()[6], original_id)
        self.assertEqual(self.row()[2], "gmail:2")
        self.assertIn("extended", self.row()[4])
        moved = self.candidate(3, "Deadline moved to 2026-11-07T09:30:00Z.")
        self.assertEqual(self.sync(moved).status, "updated")
        self.assertEqual(self.row()[6], original_id)
        self.assertEqual(len(self.service.items), 1)
        self.assertEqual(len([name for name, _ in self.service.calls if name == "patch"]), 2)

    def test_shortened_or_unexplained_date_needs_review_until_approved(self):
        self.sync(self.candidate(1, "Apply by 2026-11-05T09:30:00Z."))
        original = self.row()
        shorter = self.candidate(2, "Deadline moved earlier to 2026-11-03T09:30:00Z.")
        self.assertEqual(self.sync(shorter).status, "review")
        self.assertEqual(self.row(), original)
        unexplained = self.candidate(3, "Apply by 2026-11-07T09:30:00Z.")
        self.assertEqual(self.sync(unexplained).status, "review")
        self.assertEqual(self.row(), original)
        policy = DeadlineReviewPolicy(shorter.id, "user:1", "Recruiter confirmed earlier date",
                                      NOW, shorter.deadline_at)
        self.assertEqual(self.sync(shorter, policy=policy).status, "updated")
        self.assertEqual(self.row()[6], original[6])
        self.assertEqual(self.row()[3], "2026-11-03T09:30:00.000000Z")

    def test_insert_failure_and_patch_retry_preserve_mapping(self):
        first = self.candidate(1, "Apply by 2026-11-02T09:30:00Z.")
        self.service.failures["insert"] = [error(429)]
        from keeper_core.adapters import RateLimited
        with self.assertRaises(RateLimited):
            self.sync(first)
        self.assertIsNone(self.row()[6])
        self.assertEqual(self.sync(first).status, "created")
        original_id = self.row()[6]
        extension = self.candidate(2, "Deadline extended to 2026-11-05T09:30:00Z.")
        self.service.failures["patch"] = [error(403, "userRateLimitExceeded", retry_after=2)]
        self.assertEqual(self.sync(extension).status, "updated")
        self.assertEqual(self.row()[6], original_id)
        self.assertEqual(self.waits, [2])
        self.assertEqual(len(self.service.items), 1)

    def test_application_attached_later_gets_existing_event_history(self):
        first = self.candidate(1, "Apply by 2026-11-02T09:30:00Z.")
        created = sync_deadline_candidate(
            self.path, self.calendar, first, opportunity_id="opp:1", now=NOW)
        self.assertEqual(created.status, "created")
        self.assertIsNone(self.row()[1])
        self.assertEqual(self.sync(first).status, "unchanged")
        self.assertEqual(self.row()[1], "app:1")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            activities = conn.execute(
                "SELECT kind, source_email_id, provider_id FROM application_activity"
            ).fetchall()
        self.assertEqual(activities, [("calendar_created", "gmail:1", created.provider_event_id)])
        self.assertEqual(len(self.service.items), 1)


if __name__ == "__main__":
    unittest.main()
