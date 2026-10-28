"""Offline Calendar API tests with a real Keeper SQLite mapping."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from integrations.calendar import LiveCalendar
from keeper_core.adapters import IdempotencyConflict, RateLimited
from keeper_core.db import migrate
from keeper_core.deadlines import DeadlineReviewPolicy, DeadlineReviewRequired
from keeper_core.time import to_storage


class HttpResponse(dict):
    def __init__(self, status, retry_after=None):
        super().__init__()
        self.status = status
        if retry_after is not None:
            self["retry-after"] = str(retry_after)


def error(status, reason=None, retry_after=None):
    result = Exception("Calendar request failed")
    result.resp = HttpResponse(status, retry_after)
    result.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return result


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class Events:
    def __init__(self, backend):
        self.backend = backend

    def list(self, **kwargs):
        def answer():
            self.backend.calls.append(("list", kwargs))
            self.backend.fail("list")
            items = list(self.backend.items.values())
            if "privateExtendedProperty" in kwargs:
                key = kwargs["privateExtendedProperty"].split("=", 1)[1]
                items = [item for item in items if item.get("extendedProperties", {}).get("private", {}).get("keeperDeadlineKey") == key]
                if self.backend.hide_key_search:
                    items = []
            if "q" in kwargs:
                items = [item for item in items if kwargs["q"].casefold() in item["summary"].casefold()]
            start = int(kwargs.get("pageToken", "0"))
            selected = items[start:start + kwargs["maxResults"]]
            result = {"items": selected}
            if start + len(selected) < len(items):
                result["nextPageToken"] = str(start + len(selected))
            return result
        return Request(answer)

    def get(self, **kwargs):
        def answer():
            self.backend.calls.append(("get", kwargs))
            self.backend.fail("get")
            if kwargs["eventId"] not in self.backend.items:
                raise error(404)
            return self.backend.items[kwargs["eventId"]]
        return Request(answer)

    def insert(self, **kwargs):
        def answer():
            self.backend.calls.append(("insert", kwargs))
            self.backend.fail("insert")
            item = kwargs["body"].copy()
            if item["id"] in self.backend.items:
                raise error(409)
            self.backend.items[item["id"]] = item
            return item
        return Request(answer)

    def patch(self, **kwargs):
        def answer():
            self.backend.calls.append(("patch", kwargs))
            self.backend.fail("patch")
            item = self.backend.items[kwargs["eventId"]]
            item.update(kwargs["body"])
            return item
        return Request(answer)


class Service:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.failures = {}
        self.hide_key_search = False

    def events(self):
        return Events(self)

    def fail(self, operation):
        queue = self.failures.get(operation, [])
        if queue:
            raise queue.pop(0)


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "keeper.db"
        migrate(self.db)
        self.initial = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        stamp = to_storage(self.initial)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""INSERT INTO opportunities
                (id, dedupe_key, canonical_url, organization, title, source, discovered_at, eligibility)
                VALUES ('opp:1', 'key:1', 'https://example.test/1', 'Example', 'Intern', 'test', ?, 'eligible')""", (stamp,))
            conn.execute("""INSERT INTO deadline_events
                (id, opportunity_id, deadline_at, evidence, created_at, updated_at)
                VALUES ('deadline:1', 'opp:1', ?, 'test', ?, ?)""", (stamp, stamp, stamp))
        self.service = Service()
        self.waits = []
        self.calendar = LiveCalendar(self.service, self.db, sleep=self.waits.append,
                                     now=lambda: self.initial)

    def tearDown(self):
        self.temp.cleanup()

    def mapping(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT provider, provider_event_id, deadline_at FROM deadline_events WHERE id = 'deadline:1'").fetchone()

    def test_health_lists_one_page_without_changes(self):
        self.calendar.check_health()
        self.assertEqual([name for name, _ in self.service.calls], ["list"])
        self.assertEqual(self.service.calls[0][1]["maxResults"], 1)

    def test_past_deadline_is_blocked_before_provider_without_review_policy(self):
        past = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        with self.assertRaises(DeadlineReviewRequired):
            self.calendar.create_deadline("deadline:1", past, "Apply", idempotency_key="key:past")
        self.assertEqual(self.service.calls, [])
        self.assertIsNone(self.mapping()[1])
        policy = DeadlineReviewPolicy("candidate:reviewed", "user:1", "Approved historical event",
                                      self.initial, past, allow_past=True)
        self.calendar.create_deadline("deadline:1", past, "Apply", idempotency_key="key:past",
                                      review_policy=policy)
        self.assertEqual(len(self.service.items), 1)

    def test_create_persists_id_then_updates_same_event(self):
        event_id = self.calendar.create_deadline("deadline:1", self.initial, "Apply", idempotency_key="key:1")
        self.assertEqual(self.mapping()[:2], ("google-calendar", event_id))
        self.assertEqual(len(self.service.items), 1)
        later = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        restarted = LiveCalendar(self.service, self.db, now=lambda: later)
        self.assertEqual(restarted.upsert_deadline("deadline:1", later, "Apply later",
                                                  idempotency_key="key:1", provider_event_id=event_id), event_id)
        self.assertEqual(self.mapping()[2], to_storage(later))
        self.assertEqual(len(self.service.items), 1)
        self.assertEqual(self.service.items[event_id]["start"]["dateTime"], "2026-11-03T12:00:00Z")
        self.assertEqual(len([name for name, _ in self.service.calls if name == "patch"]), 1)

    def test_search_pagination(self):
        self.service.items = {
            str(index): {"id": str(index), "summary": "Apply now",
                         "start": {"dateTime": "2026-11-01T12:00:00Z"}}
            for index in range(3)
        }
        first = self.calendar.search_events("apply", limit=2)
        second = self.calendar.search_events("apply", cursor=first.next_cursor, limit=2)
        self.assertEqual([event.provider_event_id for event in first.events + second.events], ["0", "1", "2"])
        self.assertIsNone(second.next_cursor)
        self.assertEqual(first.events[0].starts_at.tzinfo, timezone.utc)

    def test_recovers_provider_id_after_database_write_gap(self):
        provider_id = self.calendar.create_deadline("deadline:1", self.initial, "Apply", idempotency_key="key:1")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE deadline_events SET provider = NULL, provider_event_id = NULL WHERE id = 'deadline:1'")
        restarted = LiveCalendar(self.service, self.db)
        self.assertEqual(restarted.upsert_deadline("deadline:1", self.initial, "Apply",
                                                  idempotency_key="key:1"), provider_id)
        self.assertEqual(self.mapping()[1], provider_id)
        self.assertEqual(len([name for name, _ in self.service.calls if name == "insert"]), 1)

    def test_rate_limit_and_identity_conflict(self):
        self.service.failures["list"] = [error(429, retry_after=3)]
        self.calendar.search_events("apply")
        self.assertEqual(self.waits, [3])
        self.service.failures["list"] = [error(403, "rateLimitExceeded")] * 3
        with self.assertRaises(RateLimited):
            self.calendar.search_events("apply")
        self.assertEqual(self.waits[-2:], [1, 2])
        provider_id = self.calendar.create_deadline("deadline:1", self.initial, "Apply", idempotency_key="key:1")
        with self.assertRaises(IdempotencyConflict):
            self.calendar.upsert_deadline("deadline:1", self.initial, "Apply",
                                          idempotency_key="key:1", provider_event_id="wrong")
        self.assertEqual(self.mapping()[1], provider_id)

    def test_uncertain_insert_is_not_blindly_replayed(self):
        self.service.failures["insert"] = [error(429)]
        with self.assertRaises(RateLimited):
            self.calendar.create_deadline("deadline:1", self.initial, "Apply", idempotency_key="key:2")
        self.assertEqual(len([name for name, _ in self.service.calls if name == "insert"]), 1)
        self.assertIsNone(self.mapping()[1])

    def test_deterministic_id_recovers_a_search_index_gap(self):
        provider_id = self.calendar.create_deadline("deadline:1", self.initial, "Apply", idempotency_key="key:1")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE deadline_events SET provider = NULL, provider_event_id = NULL WHERE id = 'deadline:1'")
        self.service.hide_key_search = True
        restarted = LiveCalendar(self.service, self.db)
        self.assertEqual(restarted.create_deadline("deadline:1", self.initial, "Apply",
                                                  idempotency_key="key:1"), provider_id)
        self.assertEqual(self.mapping()[1], provider_id)
        self.assertEqual(len(self.service.items), 1)

    def test_collision_recovery_applies_requested_deadline(self):
        provider_id = self.calendar.create_deadline(
            "deadline:1", self.initial, "Apply", idempotency_key="key:1")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE deadline_events SET provider = NULL, provider_event_id = NULL WHERE id = 'deadline:1'")
        self.service.hide_key_search = True
        later = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        recovered = LiveCalendar(self.service, self.db, now=lambda: self.initial)
        self.assertEqual(recovered.upsert_deadline(
            "deadline:1", later, "Apply later", idempotency_key="key:1"), provider_id)
        self.assertEqual(self.service.items[provider_id]["start"]["dateTime"], "2026-11-03T12:00:00Z")
        self.assertEqual(self.mapping()[2], to_storage(later))
        self.assertEqual(len(self.service.items), 1)

    def test_patch_rate_limit_retries_without_creating(self):
        provider_id = self.calendar.create_deadline("deadline:1", self.initial, "Apply", idempotency_key="key:1")
        self.service.failures["patch"] = [error(403, "userRateLimitExceeded", retry_after=2)]
        later = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.assertEqual(self.calendar.upsert_deadline("deadline:1", later, "Apply later",
                                                       idempotency_key="key:1"), provider_id)
        self.assertEqual(self.waits, [2])
        self.assertEqual(len([name for name, _ in self.service.calls if name == "patch"]), 2)


if __name__ == "__main__":
    unittest.main()
