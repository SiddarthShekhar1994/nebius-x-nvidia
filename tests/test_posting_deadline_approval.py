"""Offline review and retry tests for page-backed deadline scheduling."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.db import migrate
from keeper_core.deadlines import DeadlineReviewPolicy, DeadlineReviewRequired
from keeper_core.radar_deadlines import (
    VerifiedPostingDeadline,
    posting_deadline_revision,
    sync_posting_deadline,
)
from keeper_core.time import from_storage, to_storage
from keeper_core.workflow import stable_key


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
URL = "https://jobs.example.test/apply/1"


def page_deadline(day: int, excerpt: str | None = None) -> VerifiedPostingDeadline:
    raw = f"2026-11-{day:02d}T23:59:00Z"
    return VerifiedPostingDeadline(
        URL, excerpt or f"Applications close {raw}", raw,
        datetime(2026, 11, day, 23, 59, tzinfo=timezone.utc),
    )


def approval(opportunity_id: str, deadline: VerifiedPostingDeadline, *, approved_at=NOW):
    return DeadlineReviewPolicy(
        posting_deadline_revision(opportunity_id, deadline), "user:1", "Confirmed on application page",
        approved_at, deadline.deadline_at,
    )


class PersistingCalendar:
    """Fake adapter with LiveCalendar's mapping-first persistence behavior."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.calls = []
        self.events = {}
        self.fail_before_mapping = False

    def upsert_deadline(self, event_id, deadline_at, summary, *, idempotency_key,
                        provider_event_id=None, review_policy=None):
        self.calls.append((event_id, deadline_at, provider_event_id, review_policy))
        if self.fail_before_mapping:
            self.fail_before_mapping = False
            raise RuntimeError("calendar unavailable")
        existing = self.events.get(event_id)
        if existing is None:
            provider_id = f"provider:{len(self.events) + 1}"
        else:
            provider_id = existing[0]
            if provider_event_id not in (None, provider_id):
                raise AssertionError("provider ID changed")
        self.events[event_id] = (provider_id, deadline_at, summary)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """UPDATE deadline_events SET provider = 'google-calendar', provider_event_id = ?,
                   deadline_at = ?, updated_at = ? WHERE id = ?""",
                (provider_id, to_storage(deadline_at), to_storage(NOW), event_id),
            )
        return provider_id


class PostingDeadlineApprovalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "keeper.db"
        migrate(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """INSERT INTO opportunities
                   (id, dedupe_key, canonical_url, organization, title, source, discovered_at, eligibility)
                   VALUES ('opp:1', 'key:1', ?, 'Example', 'Intern', 'radar', ?, 'eligible')""",
                (URL, to_storage(NOW)),
            )
        self.calendar = PersistingCalendar(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def sync(self, deadline, *, policy=None, now=NOW):
        return sync_posting_deadline(self.db, self.calendar, opportunity_id="opp:1",
                                     deadline=deadline, now=now, policy=policy)

    def row(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT id, deadline_at, evidence, provider, provider_event_id FROM deadline_events"
            ).fetchone()

    def test_revision_uses_canonical_page_and_exact_timestamp_and_excerpt(self):
        first = page_deadline(1)
        with_query = VerifiedPostingDeadline(
            URL + "?utm_source=radar", first.source_excerpt, first.raw_timestamp, first.deadline_at,
        )
        expected = stable_key("posting-deadline", "opp:1", URL, first.raw_timestamp, first.source_excerpt)
        self.assertEqual(posting_deadline_revision("opp:1", first), expected)
        self.assertEqual(posting_deadline_revision("opp:1", with_query), expected)
        self.assertNotEqual(posting_deadline_revision("opp:1", page_deadline(1, "Confirmed " + first.source_excerpt)), expected)

    def test_mismatched_or_future_policy_fails_before_database_or_provider_writes(self):
        deadline = page_deadline(1)
        wrong_revision = DeadlineReviewPolicy(
            "stale-revision", "user:1", "Confirmed", NOW, deadline.deadline_at,
        )
        wrong_instant = DeadlineReviewPolicy(
            posting_deadline_revision("opp:1", deadline), "user:1", "Confirmed", NOW,
            deadline.deadline_at + timedelta(minutes=1),
        )
        future_approval = approval("opp:1", deadline, approved_at=NOW + timedelta(seconds=1))
        for policy in (wrong_revision, wrong_instant, future_approval):
            with self.subTest(policy=policy), self.assertRaises(DeadlineReviewRequired):
                self.sync(deadline, policy=policy)
        self.assertIsNone(self.row())
        self.assertEqual(self.calendar.calls, [])

    def test_new_deadline_and_pending_retry_need_policy_and_confirmed_eligibility(self):
        first = page_deadline(1)
        self.assertEqual(self.sync(first).status, "review")
        self.assertIsNone(self.row())
        self.assertEqual(self.calendar.calls, [])

        self.calendar.fail_before_mapping = True
        with self.assertRaisesRegex(RuntimeError, "calendar unavailable"):
            self.sync(first, policy=approval("opp:1", first))
        pending = self.row()
        call_count = len(self.calendar.calls)
        self.assertEqual(self.sync(first).status, "review")
        self.assertEqual(self.row(), pending)
        self.assertEqual(len(self.calendar.calls), call_count)

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE opportunities SET eligibility = 'unverified' WHERE id = 'opp:1'")
        self.assertEqual(self.sync(first, policy=approval("opp:1", first)).status, "review")
        self.assertEqual(self.row(), pending)
        self.assertEqual(len(self.calendar.calls), call_count)

    def test_approved_create_and_changed_date_reuse_provider_event(self):
        first = page_deadline(1)
        created = self.sync(first, policy=approval("opp:1", first))
        original = self.row()
        self.assertEqual(created.status, "created")
        self.assertEqual(self.calendar.calls[-1][3], approval("opp:1", first))

        self.assertEqual(self.sync(first).status, "unchanged")
        moved = page_deadline(2)
        call_count = len(self.calendar.calls)
        self.assertEqual(self.sync(moved).status, "review")
        self.assertEqual(self.row(), original)
        self.assertEqual(len(self.calendar.calls), call_count)

        moved_approval = approval("opp:1", moved)
        updated = self.sync(moved, policy=moved_approval)
        row = self.row()
        self.assertEqual(updated.status, "updated")
        self.assertEqual(row[0], original[0])
        self.assertEqual(row[1], to_storage(moved.deadline_at))
        self.assertEqual(row[2], f"{URL} | {moved.source_excerpt}")
        self.assertEqual(row[4], original[4])
        with closing(sqlite3.connect(self.db)) as conn:
            opportunity = conn.execute(
                "SELECT deadline, deadline_evidence FROM opportunities WHERE id = 'opp:1'"
            ).fetchone()
        self.assertEqual(from_storage(opportunity[0]), moved.deadline_at)
        self.assertEqual(opportunity[1], row[2])

    def test_pending_creation_outage_retries_same_date_but_cannot_change_date(self):
        first = page_deadline(1)
        self.calendar.fail_before_mapping = True
        with self.assertRaisesRegex(RuntimeError, "calendar unavailable"):
            self.sync(first, policy=approval("opp:1", first))
        pending = self.row()
        self.assertIsNone(pending[4])

        moved = page_deadline(2)
        count = len(self.calendar.calls)
        self.assertEqual(self.sync(moved, policy=approval("opp:1", moved)).status, "review")
        self.assertEqual(self.row(), pending)
        self.assertEqual(len(self.calendar.calls), count)

        self.assertEqual(self.sync(first, policy=approval("opp:1", first)).status, "created")
        self.assertEqual(self.row()[0], pending[0])
        self.assertEqual(len(self.calendar.events), 1)

    def test_exact_approved_replay_repairs_mapping_first_crash_evidence(self):
        first = page_deadline(1)
        self.sync(first, policy=approval("opp:1", first))
        before = self.row()
        moved = page_deadline(2)
        moved_policy = approval("opp:1", moved)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """CREATE TRIGGER fail_evidence_update BEFORE UPDATE OF evidence ON deadline_events
                   WHEN NEW.evidence != OLD.evidence
                   BEGIN SELECT RAISE(ABORT, 'injected evidence write failure'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected evidence write failure"):
            self.sync(moved, policy=moved_policy)
        mapped = self.row()
        self.assertEqual(mapped[1], to_storage(moved.deadline_at))
        self.assertEqual(mapped[2], before[2])
        self.assertEqual(mapped[4], before[4])

        stale_policy = approval("opp:1", moved, approved_at=NOW)
        stale_revision = DeadlineReviewPolicy(
            "different-page-revision", stale_policy.approved_by, stale_policy.reason,
            stale_policy.approved_at, stale_policy.confirmed_at,
        )
        call_count = len(self.calendar.calls)
        with self.assertRaises(DeadlineReviewRequired):
            self.sync(moved, policy=stale_revision)
        self.assertEqual(len(self.calendar.calls), call_count)

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TRIGGER fail_evidence_update")
        replayed = self.sync(moved, policy=moved_policy)
        repaired = self.row()
        self.assertEqual(replayed.status, "updated")
        self.assertEqual(repaired[2], f"{URL} | {moved.source_excerpt}")
        self.assertEqual(repaired[4], before[4])
        self.assertEqual(len(self.calendar.events), 1)


if __name__ == "__main__":
    unittest.main()
