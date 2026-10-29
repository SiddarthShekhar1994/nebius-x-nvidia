"""Offline tests for human-reviewed page deadlines and Calendar approval."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from keeper_core.contracts import Eligibility
from keeper_core.db import migrate
from keeper_core.deadlines import DeadlineReviewRequired
from keeper_core.posting_calendar import calendar_review_plan, schedule_reviewed_deadline
from keeper_core.posting_reviews import get_posting_review, parse_posting_evidence, save_posting_review
from keeper_core.radar import load_profile
from keeper_core.time import from_storage, to_storage


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "demo" / "profile.md"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
URL = "https://jobs.lever.co/acme/123"


def payload(day=20, *, complete=True, graduation_years=(2027,), deadline=True):
    raw = f"2026-11-{day:02d}T17:00:00Z" if deadline else None
    excerpt = f"2027 Computer Science graduates in United States. Apply by {raw}." if raw else (
        "2027 Computer Science graduates in United States."
    )
    return {
        "opportunity_id": "opp:1",
        "source_url": URL,
        "source_excerpt": excerpt,
        "requirements": {
            "complete": complete,
            "graduation_years": list(graduation_years),
            "majors": ["Computer Science"],
            "locations": ["United States"],
        },
        "deadline_timestamp": raw,
    }


class FakeCalendar:
    """Persist the same provider mapping the live adapter would save."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = []
        self.events = {}
        self.fail_once = False

    def upsert_deadline(self, event_id, deadline_at, summary, *, idempotency_key,
                        provider_event_id=None, review_policy=None):
        self.calls.append((event_id, deadline_at, idempotency_key, provider_event_id, review_policy))
        if review_policy is not None:
            with closing(sqlite3.connect(self.db_path)) as conn:
                saved = conn.execute(
                    """SELECT approved_by, approved_at, approval_reason FROM posting_reviews
                       ORDER BY reviewed_at DESC, rowid DESC LIMIT 1"""
                ).fetchone()
            if saved != (review_policy.approved_by, to_storage(review_policy.approved_at),
                         review_policy.reason):
                raise AssertionError("approval was not committed before the Calendar call")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("calendar unavailable")
        existing = self.events.get(event_id)
        if existing is None:
            provider_id = f"provider:{len(self.events) + 1}"
        else:
            provider_id = existing[0]
            if provider_event_id not in (None, provider_id):
                raise AssertionError("unexpected provider ID")
        self.events[event_id] = (provider_id, deadline_at, summary)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """UPDATE deadline_events SET provider = 'google-calendar', provider_event_id = ?,
                   deadline_at = ?, updated_at = ? WHERE id = ?""",
                (provider_id, to_storage(deadline_at), to_storage(NOW), event_id),
            )
        return provider_id


class PostingCalendarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "keeper.db"
        migrate(self.db)
        self.profile = load_profile(PROFILE)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """INSERT INTO opportunities
                   (id, dedupe_key, canonical_url, organization, title, source, discovered_at, eligibility)
                   VALUES ('opp:1', 'key:1', ?, 'Acme', 'Software Engineer Intern',
                           'jobs.lever.co', ?, 'unverified')""",
                (URL, to_storage(NOW)),
            )
        self.calendar = FakeCalendar(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, review_id, *, data=None, now=NOW, reviewer="user:vi"):
        evidence = parse_posting_evidence(data or payload())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            return save_posting_review(conn, self.profile, evidence, review_id=review_id,
                                       reviewed_by=reviewer, now=now)

    def plan(self, review_id, *, now=NOW):
        with closing(sqlite3.connect(self.db)) as conn:
            return calendar_review_plan(conn, review_id, now=now)

    def schedule(self, review_id, *, reviewer="user:vi", reason="Confirmed the page", now=NOW):
        return schedule_reviewed_deadline(self.db, self.calendar, review_id,
                                          approved_by=reviewer, reason=reason, now=now)

    def event_rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT id, deadline_at, evidence, provider, provider_event_id FROM deadline_events"
            ).fetchall()

    def test_preview_has_plan_and_does_not_approve_or_write_calendar(self):
        saved = self.save("review:1")
        before = self.event_rows()
        plan = self.plan("review:1")
        self.assertEqual(plan["action"], "create")
        self.assertEqual(plan["review_id"], "review:1")
        self.assertEqual(plan["deadline_at"], datetime(2026, 11, 20, 17, tzinfo=timezone.utc))
        self.assertIsNone(plan["provider_event_id"])
        self.assertIsNone(saved["approved_by"])
        self.assertEqual(self.event_rows(), before)
        self.assertEqual(self.calendar.calls, [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(get_posting_review(conn, "review:1")["approved_at"])

    def test_missing_partial_ineligible_stale_past_and_missing_deadline_are_blocked(self):
        with self.assertRaises(ValueError):
            self.plan("review:missing")
        with self.assertRaises(ValueError):
            self.schedule("review:missing")

        cases = (
            ("review:partial", payload(complete=False)),
            ("review:ineligible", payload(graduation_years=(2028,))),
            ("review:past", {
                **payload(), "source_excerpt": "Apply by 2026-10-01T17:00:00Z.",
                "deadline_timestamp": "2026-10-01T17:00:00Z",
            }),
            ("review:no-deadline", payload(deadline=False)),
        )
        for review_id, data in cases:
            with self.subTest(review_id=review_id):
                saved = self.save(review_id, data=data)
                with self.assertRaises(DeadlineReviewRequired):
                    self.plan(review_id)
                with self.assertRaises(DeadlineReviewRequired):
                    self.schedule(review_id)
                self.assertIsNone(saved["approved_by"])
                self.assertEqual(self.event_rows(), [])
                self.assertEqual(self.calendar.calls, [])

        self.save("review:latest-old", now=NOW)
        self.save("review:latest-new", now=NOW.replace(minute=30), data=payload(21))
        with self.assertRaises(DeadlineReviewRequired):
            self.plan("review:latest-old")
        with self.assertRaises(DeadlineReviewRequired):
            self.schedule("review:latest-old")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(get_posting_review(conn, "review:latest-old")["approved_by"])
            self.assertIsNone(get_posting_review(conn, "review:latest-new")["approved_by"])
        self.assertEqual(self.event_rows(), [])
        self.assertEqual(self.calendar.calls, [])

    def test_approval_is_durable_retryable_and_updated_review_reuses_event(self):
        first = self.save("review:1")
        self.assertEqual(first["eligibility"], Eligibility.ELIGIBLE)
        created = self.schedule("review:1")
        first_row = self.event_rows()[0]
        self.assertEqual(created.status, "created")
        self.assertEqual(first_row[4], created.provider_event_id)

        replayed = self.schedule("review:1")
        self.assertEqual(replayed.status, "unchanged")
        self.assertEqual(self.event_rows()[0][4], first_row[4])
        with closing(sqlite3.connect(self.db)) as conn:
            approved = get_posting_review(conn, "review:1")
        self.assertEqual((approved["approved_by"], approved["approval_reason"]),
                         ("user:vi", "Confirmed the page"))
        self.assertEqual(approved["approved_at"], NOW)

        second = self.save("review:2", data=payload(21), now=NOW.replace(minute=10))
        self.assertIsNone(second["approved_by"])
        plan = self.plan("review:2", now=NOW.replace(minute=10))
        self.assertEqual(plan["action"], "update")
        self.assertEqual(plan["provider_event_id"], first_row[4])
        updated = self.schedule("review:2", now=NOW.replace(minute=11))
        second_row = self.event_rows()[0]
        self.assertEqual(updated.status, "updated")
        self.assertEqual(second_row[4], first_row[4])
        self.assertEqual(from_storage(second_row[1]), datetime(2026, 11, 21, 17, tzinfo=timezone.utc))
        self.assertEqual(second_row[2], f"{URL} | {payload(21)['source_excerpt']}")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(get_posting_review(conn, "review:2")["approved_at"], NOW.replace(minute=11))
        self.assertEqual(len(self.calendar.events), 1)

    def test_provider_outage_keeps_saved_approval_and_pending_event_for_retry(self):
        self.save("review:1")
        self.calendar.fail_once = True
        with self.assertRaisesRegex(RuntimeError, "calendar unavailable"):
            self.schedule("review:1")
        pending = self.event_rows()[0]
        self.assertIsNone(pending[3])
        self.assertIsNone(pending[4])
        with closing(sqlite3.connect(self.db)) as conn:
            approval = get_posting_review(conn, "review:1")
        self.assertEqual((approval["approved_by"], approval["approved_at"], approval["approval_reason"]),
                         ("user:vi", NOW, "Confirmed the page"))

        retried = self.schedule("review:1", now=NOW.replace(minute=5))
        self.assertEqual(retried.status, "created")
        self.assertEqual(self.event_rows()[0][4], retried.provider_event_id)
        self.assertEqual(len(self.calendar.events), 1)

    def test_saved_approval_is_immutable_and_old_review_cannot_overwrite_latest(self):
        self.save("review:1")
        self.schedule("review:1")
        call_count = len(self.calendar.calls)
        with self.assertRaises(DeadlineReviewRequired):
            self.schedule("review:1", reviewer="user:other")
        with self.assertRaises(DeadlineReviewRequired):
            self.schedule("review:1", reason="A different reason")
        self.assertEqual(len(self.calendar.calls), call_count)

        self.save("review:2", data=payload(21), now=NOW.replace(minute=10))
        before = self.event_rows()
        with self.assertRaises(DeadlineReviewRequired):
            self.schedule("review:1", now=NOW.replace(minute=11))
        self.assertEqual(self.event_rows(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(get_posting_review(conn, "review:1")["approved_at"], NOW)
            self.assertIsNone(get_posting_review(conn, "review:2")["approved_at"])
        self.assertEqual(len(self.calendar.calls), call_count)


if __name__ == "__main__":
    unittest.main()
