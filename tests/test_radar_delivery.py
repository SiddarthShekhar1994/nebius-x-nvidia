import json
import sqlite3
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from keeper_core.adapters import ModelAnswer, ProviderUnavailable, SearchHit
from keeper_core.contracts import Eligibility, ModelMetadata
from keeper_core.fakes import FakeCalendar, FakeTelegram, FakeTavily
from keeper_core import radar_digest
from keeper_core.radar import build_queries, load_profile
from keeper_core.radar_digest import send_radar_digest
from keeper_core.radar_deadlines import VerifiedPostingDeadline
from keeper_core.radar_delivery import run_daily_radar
from keeper_core.radar_enrichment import VerifiedPostingRequirements
from keeper_core.posting_calendar import schedule_reviewed_deadline
from keeper_core.posting_reviews import parse_posting_evidence, save_posting_review
from keeper_core.time import from_storage
from keeper_core.workflow import canonical_url


PROFILE = Path(__file__).resolve().parents[1] / "demo" / "profile.md"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def hit(number, *, title=None, snippet="Build developer tools."):
    return SearchHit(
        f"https://jobs.lever.co/org{number}/{number}?utm_source=radar",
        title or f"Software Engineer Intern at Org{number}", snippet, "jobs.lever.co",
    )


class Model:
    def __init__(self, scores=None):
        self.scores = scores or {}
        self.calls = 0

    def complete(self, task_type, prompt):
        self.calls += 1
        self_source = json.loads(prompt.split(" SOURCE_JSON: ", 1)[1])
        score = self.scores.get(self_source["organization"], 8)
        return ModelAnswer(
            json.dumps({"score": score, "evidence": "Software Engineer Intern",
                        "reason": "Matches the target software role"}),
            ModelMetadata("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                          "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "test"),
        )


class FlakyTelegram(FakeTelegram):
    fail_once = True

    def send_message(self, recipient_reference, text, *, idempotency_key):
        if self.fail_once:
            self.fail_once = False
            raise ProviderUnavailable("offline")
        return super().send_message(recipient_reference, text, idempotency_key=idempotency_key)


class CountingTelegram(FakeTelegram):
    calls = 0

    def send_message(self, recipient_reference, text, *, idempotency_key):
        self.calls += 1
        return super().send_message(recipient_reference, text, idempotency_key=idempotency_key)


class RadarDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db = Path(self.tmp.name) / "keeper.db"
        self.profile = load_profile(PROFILE)
        self.query = build_queries(self.profile, max_queries=1)[0]
        self.telegram = FakeTelegram()
        self.calendar = FakeCalendar()
        self.model = Model()

    def tearDown(self):
        self.tmp.cleanup()

    def run_radar(self, hits, *, run_id="day-one", now=NOW, requirements=None,
                  deadlines=None, telegram=None, searcher=None):
        tavily = searcher or FakeTavily({self.query: tuple(hits)})
        requirements_by_url = {
            canonical_url(source.url): VerifiedPostingRequirements(
                canonical_url(source.url), "Full requirements reviewed", True
            )
            for source in hits
        }
        requirements_by_url.update({canonical_url(url): item for url, item in (requirements or {}).items()})
        return run_daily_radar(
            self.db, self.profile, tavily, self.model, telegram or self.telegram,
            recipient_reference="demo-chat", run_id=run_id, now=now,
            calendar=self.calendar, verified_requirements=requirements_by_url,
            verified_deadlines=deadlines, max_queries=1, results_per_query=10,
        )

    def count(self, table):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def save_deadline_review(self, deadline, review_id, *, now):
        evidence = parse_posting_evidence({
            "opportunity_id": self._opportunity_id(),
            "source_url": deadline.source_url,
            "source_excerpt": deadline.source_excerpt,
            "requirements": {
                "complete": True,
                "graduation_years": [2027],
                "majors": ["Computer Science"],
                "locations": ["United States"],
            },
            "deadline_timestamp": deadline.raw_timestamp,
        })
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            return save_posting_review(conn, self.profile, evidence, review_id=review_id,
                                       reviewed_by="user:vi", now=now)

    def test_empty_results_send_nothing(self):
        outcome = self.run_radar(())
        self.assertEqual(outcome.digest.status, "empty")
        self.assertEqual(outcome.radar.results.opportunities, ())
        self.assertEqual(self.count("opportunities"), 0)
        self.assertEqual(self.count("radar_digest_snapshots"), 0)
        self.assertEqual(self.telegram.messages_by_key, {})

    def test_search_outage_does_not_persist_or_send(self):
        class Outage:
            def search(self, query, *, limit=5):
                raise ProviderUnavailable("search unavailable")

        with self.assertRaises(ProviderUnavailable):
            self.run_radar((), searcher=Outage())
        self.assertEqual(self.count("opportunities"), 0)
        self.assertEqual(self.telegram.messages_by_key, {})

    def test_duplicate_urls_and_irrelevant_jobs_are_skipped(self):
        first = hit(1)
        duplicate = SearchHit(first.url.replace("?utm_source=radar", "#details"),
                              first.title, first.snippet, first.source)
        irrelevant = hit(2, title="Marketing Intern at Org2")
        outcome = self.run_radar((first, duplicate, irrelevant))
        self.assertEqual(self.count("opportunities"), 1)
        self.assertEqual(self.count("opportunity_observations"), 1)
        self.assertEqual(len(outcome.digest.opportunity_ids), 1)
        self.assertEqual(len(self.telegram.messages_by_key), 1)
        self.assertTrue(any("duplicate" in item.reason for item in outcome.radar.results.skipped))
        self.assertTrue(any("target roles" in item.reason for item in outcome.radar.results.skipped))

    def test_top_five_sorted_with_scores_links_and_reasons(self):
        self.model = Model({f"Org{i}": score for i, score in enumerate((10, 9, 8, 7, 7, 6, 5), 1)})
        outcome = self.run_radar(tuple(hit(i) for i in range(1, 8)))
        self.assertEqual(len(outcome.digest.opportunity_ids), 5)
        self.assertEqual(self.count("opportunities"), 7)
        body = outcome.digest.text
        for i in range(1, 6):
            self.assertIn(f"Org{i}", body)
            expected_scores = (10, 9, 8, 7, 7)
            self.assertIn(f"{expected_scores[i - 1]}/10", body)
            self.assertIn(f"https://jobs.lever.co/org{i}/{i}", body)
        self.assertNotIn("Org6", body)
        self.assertNotIn("Org7", body)
        self.assertIn("Matches the target software role", body)
        self.assertLessEqual(len(body), 4096)

    def test_digest_requires_score_seven_and_verified_eligibility(self):
        self.model = Model({"Org1": 6, "Org2": 7, "Org3": 10})
        uncertain = VerifiedPostingRequirements(
            "https://jobs.lever.co/org3/3", "US citizens only", True,
            authorization="us_citizen",
        )
        outcome = self.run_radar(tuple(hit(i) for i in range(1, 4)),
                                 requirements={uncertain.source_url: uncertain})
        self.assertEqual(outcome.digest.status, "sent")
        self.assertEqual(len(outcome.digest.opportunity_ids), 1)
        self.assertIn("Org2", outcome.digest.text)
        self.assertNotIn("Org1", outcome.digest.text)
        self.assertNotIn("Org3", outcome.digest.text)
        self.assertEqual(len(self.telegram.messages_by_key), 1)

    def test_unknown_authorization_stays_unverified(self):
        source = hit(1)
        requirements = VerifiedPostingRequirements(
            "https://jobs.lever.co/org1/1", "US citizens only", True,
            authorization="us_citizen",
        )
        outcome = self.run_radar((source,), requirements={requirements.source_url: requirements})
        opportunity = outcome.radar.results.opportunities[0]
        self.assertEqual(opportunity.eligibility, Eligibility.UNVERIFIED)
        self.assertIn("not voluntarily supplied", opportunity.eligibility_reason)
        self.assertEqual(outcome.digest.status, "empty")
        self.assertIsNone(outcome.digest.text)
        self.assertEqual(self.telegram.messages_by_key, {})

    def test_ineligible_candidate_is_persisted_but_not_digested(self):
        source = hit(1)
        requirements = VerifiedPostingRequirements(
            "https://jobs.lever.co/org1/1", "Graduating class of 2028", True,
            graduation_years=(2028,),
        )
        outcome = self.run_radar((source,), requirements={requirements.source_url: requirements})
        self.assertEqual(outcome.radar.results.opportunities[0].eligibility, Eligibility.INELIGIBLE)
        self.assertEqual(self.count("opportunities"), 1)
        self.assertEqual(outcome.digest.status, "empty")

    def test_repeat_runs_do_not_duplicate_digest_or_history(self):
        source = hit(1)
        first = self.run_radar((source,))
        original_body = first.digest.text
        self.model = Model({"Org1": 6})
        same_run = self.run_radar((source,), now=NOW + timedelta(minutes=5))
        self.assertEqual(same_run.digest.message_id, first.digest.message_id)
        self.assertEqual(same_run.digest.text, original_body)
        self.assertEqual(self.count("opportunity_observations"), 1)
        self.assertEqual(len(self.telegram.messages_by_key), 1)
        next_day = self.run_radar((source,), run_id="day-two", now=NOW + timedelta(days=1))
        self.assertEqual(next_day.digest.status, "empty")
        self.assertEqual(self.count("opportunities"), 1)
        self.assertEqual(self.count("opportunity_observations"), 2)
        self.assertEqual(len(self.telegram.messages_by_key), 1)

    def test_delivery_failure_keeps_frozen_digest_for_retry(self):
        telegram = FlakyTelegram()
        with self.assertRaises(ProviderUnavailable):
            self.run_radar((hit(1),), telegram=telegram)
        self.assertEqual(self.count("radar_digest_snapshots"), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            other_run = send_radar_digest(conn, telegram, run_id="day-two",
                                          recipient_reference="demo-chat", now=NOW)
            other_recipient = send_radar_digest(conn, telegram, run_id="day-two",
                                                recipient_reference="another-chat", now=NOW)
        self.assertEqual(other_run.status, "empty")
        self.assertEqual(other_recipient.status, "sent")
        self.model = Model({"Org1": 4})
        outcome = self.run_radar((hit(1),), now=NOW + timedelta(minutes=3), telegram=telegram)
        self.assertIn("8/10", outcome.digest.text)
        self.assertEqual(len(telegram.messages_by_key), 2)

    def test_later_verified_review_releases_candidate_once(self):
        source = hit(1)
        partial = VerifiedPostingRequirements(source.url, "Requirements partially checked", False)
        first = self.run_radar((source,), requirements={source.url: partial})
        self.assertEqual(first.digest.status, "empty")
        self.assertEqual(self.telegram.messages_by_key, {})
        reviewed = VerifiedPostingRequirements(source.url, "Full requirements reviewed", True)
        second = self.run_radar((source,), run_id="reviewed-day", now=NOW + timedelta(days=1),
                                requirements={source.url: reviewed})
        self.assertEqual(second.digest.status, "sent")
        third = self.run_radar((source,), run_id="later-day", now=NOW + timedelta(days=2),
                               requirements={source.url: reviewed})
        self.assertEqual(third.digest.status, "empty")
        self.assertEqual(len(self.telegram.messages_by_key), 1)

    def test_overlapping_digest_runs_reserve_candidate_once_per_recipient(self):
        source = hit(1)
        partial = VerifiedPostingRequirements(source.url, "Requirements partially checked", False)
        self.assertEqual(self.run_radar((source,), requirements={source.url: partial}).digest.status,
                         "empty")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE opportunities SET eligibility = 'eligible'")

        first_selected = threading.Event()
        second_selected = threading.Event()
        second_progress = threading.Event()
        release_first = threading.Event()
        original_select = radar_digest._new_candidates
        results = {}
        errors = []
        conn_one = sqlite3.connect(self.db, timeout=5, check_same_thread=False)
        conn_two = sqlite3.connect(self.db, timeout=5, check_same_thread=False)
        conn_two.set_trace_callback(
            lambda statement: second_progress.set()
            if statement.upper().startswith("BEGIN IMMEDIATE") else None
        )

        def synchronized_select(conn, recipient_reference):
            rows = original_select(conn, recipient_reference)
            if threading.current_thread().name == "digest-first":
                first_selected.set()
                if not release_first.wait(5):
                    raise TimeoutError("test did not release the first digest selection")
            elif threading.current_thread().name == "digest-second":
                second_selected.set()
                second_progress.set()
            return rows

        def send(conn, run_id):
            try:
                results[run_id] = send_radar_digest(
                    conn, self.telegram, run_id=run_id,
                    recipient_reference="demo-chat", now=NOW,
                )
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=send, args=(conn_one, "digest-one"), name="digest-first")
        second = threading.Thread(target=send, args=(conn_two, "digest-two"), name="digest-second")
        radar_digest._new_candidates = synchronized_select
        try:
            first.start()
            self.assertTrue(first_selected.wait(5), "first digest did not reach candidate selection")
            second.start()
            self.assertTrue(second_progress.wait(5), "second digest did not attempt its transaction")
            self.assertFalse(second_selected.is_set(), "second digest selected before the first reservation")
            release_first.set()
            first.join(5)
            second.join(5)
        finally:
            release_first.set()
            radar_digest._new_candidates = original_select
            conn_one.close()
            conn_two.close()
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result.status for result in results.values()), ["empty", "sent"])
        self.assertEqual(len(self.telegram.messages_by_key), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute(
                "SELECT opportunity_ids_json FROM radar_digest_snapshots WHERE recipient_reference = ?",
                ("demo-chat",),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0]), [self._opportunity_id()])

    def _opportunity_id(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT id FROM opportunities").fetchone()[0]

    def test_legacy_and_now_unsafe_snapshots_require_review_without_send(self):
        source = hit(1)
        telegram = CountingTelegram()
        first = self.run_radar((source,), telegram=telegram)
        self.assertEqual(first.digest.status, "sent")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE radar_digest_snapshots SET policy_version = 'legacy'")
        before = telegram.calls
        legacy = self.run_radar((source,), now=NOW + timedelta(minutes=1), telegram=telegram)
        self.assertEqual(legacy.digest.status, "review")
        self.assertIsNone(legacy.digest.text)
        self.assertEqual(telegram.calls, before)

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE radar_digest_snapshots SET policy_version = 'eligible-score-7-v1'")
            conn.execute("UPDATE opportunities SET score = 6")
        self.model = Model({"Org1": 6})
        unsafe = self.run_radar((source,), now=NOW + timedelta(minutes=2), telegram=telegram)
        self.assertEqual(unsafe.digest.status, "review")
        self.assertIsNone(unsafe.digest.text)
        self.assertEqual(telegram.calls, before)

    def test_only_latest_approved_page_review_reaches_calendar(self):
        class CountingCalendar(FakeCalendar):
            def __init__(self):
                super().__init__()
                self.calls = []

            def upsert_deadline(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return super().upsert_deadline(*args, **kwargs)

        self.calendar = CountingCalendar()
        raw = "2026-11-01T23:59:00Z"
        source = hit(1, snippet=f"Apply by {raw}")
        first = self.run_radar((source,))
        self.assertEqual(first.deadlines, ())
        self.assertEqual(self.calendar.events_by_key, {})
        verified = VerifiedPostingDeadline(
            "https://jobs.lever.co/org1/1", f"Applications close {raw}", raw,
            datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc),
        )
        unreviewed = self.run_radar((source,), deadlines={verified.source_url: verified},
                                    now=NOW + timedelta(minutes=1))
        self.assertEqual(unreviewed.deadlines[0][1].status, "review")
        self.assertEqual(self.calendar.events_by_key, {})
        self.assertEqual(self.calendar.calls, [])
        self.assertEqual(self.count("deadline_events"), 0)

        self.save_deadline_review(verified, "posting-review:1", now=NOW + timedelta(minutes=2))
        unapproved = self.run_radar((source,), deadlines={verified.source_url: verified},
                                    now=NOW + timedelta(minutes=3))
        self.assertEqual(unapproved.deadlines[0][1].status, "review")
        self.assertEqual(self.calendar.events_by_key, {})
        self.assertEqual(self.calendar.calls, [])

        created = schedule_reviewed_deadline(
            self.db, self.calendar, "posting-review:1", approved_by="user:vi",
            reason="Confirmed the deadline on the application page", now=NOW + timedelta(minutes=4),
        )
        self.assertEqual(created.status, "created")
        self.assertEqual(len(self.calendar.events_by_key), 1)
        self.assertEqual(self.count("deadline_events"), 1)
        provider_id = created.provider_event_id

        approved_hook_retry = self.run_radar(
            (source,), deadlines={verified.source_url: verified}, now=NOW + timedelta(minutes=5),
        )
        self.assertEqual(approved_hook_retry.deadlines[0][1].status, "unchanged")
        self.assertEqual(self.calendar.events_by_key[next(iter(self.calendar.events_by_key))][0], provider_id)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE opportunities SET deadline = NULL, deadline_evidence = NULL")
        same = self.run_radar((source,), deadlines={verified.source_url: verified},
                              now=NOW + timedelta(minutes=6))
        self.assertEqual(same.deadlines[0][1].status, "unchanged")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(from_storage(conn.execute("SELECT deadline FROM opportunities").fetchone()[0]),
                             verified.deadline_at)
        moved_raw = "2026-11-02T23:59:00Z"
        moved = VerifiedPostingDeadline(verified.source_url, f"Applications close {moved_raw}",
                                        moved_raw, datetime(2026, 11, 2, 23, 59, tzinfo=timezone.utc))
        self.save_deadline_review(moved, "posting-review:2", now=NOW + timedelta(minutes=7))
        review = self.run_radar((source,), deadlines={moved.source_url: moved},
                                now=NOW + timedelta(minutes=8))
        self.assertEqual(review.deadlines[0][1].status, "review")
        with closing(sqlite3.connect(self.db)) as conn:
            stored = conn.execute("SELECT deadline FROM opportunities").fetchone()[0]
        self.assertEqual(from_storage(stored), verified.deadline_at)
        updated = schedule_reviewed_deadline(
            self.db, self.calendar, "posting-review:2", approved_by="user:vi",
            reason="Confirmed the changed deadline on the application page", now=NOW + timedelta(minutes=9),
        )
        self.assertEqual(updated.status, "updated")
        self.assertEqual(updated.provider_event_id, provider_id)
        self.assertEqual(len(self.calendar.events_by_key), 1)
        with self.assertRaises(ValueError):
            VerifiedPostingDeadline(verified.source_url, "Apply by 2026-11-01T23:59", "2026-11-01T23:59",
                                    verified.deadline_at)

    def test_calendar_failure_leaves_retryable_pending_event(self):
        source = hit(1)
        raw = "2026-11-01T23:59:00Z"
        verified = VerifiedPostingDeadline(
            "https://jobs.lever.co/org1/1", f"Applications close {raw}", raw,
            datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc),
        )

        class FlakyCalendar(FakeCalendar):
            fail_once = True

            def upsert_deadline(self, *args, **kwargs):
                if self.fail_once:
                    self.fail_once = False
                    raise ProviderUnavailable("calendar unavailable")
                return super().upsert_deadline(*args, **kwargs)

        self.calendar = FlakyCalendar()
        self.run_radar((source,))
        self.save_deadline_review(verified, "posting-review:retry", now=NOW + timedelta(minutes=1))
        with self.assertRaises(ProviderUnavailable):
            schedule_reviewed_deadline(
                self.db, self.calendar, "posting-review:retry", approved_by="user:vi",
                reason="Confirmed the deadline on the application page", now=NOW + timedelta(minutes=2),
            )
        self.assertEqual(self.count("deadline_events"), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT provider_event_id FROM deadline_events").fetchone()[0])
            saved = conn.execute(
                "SELECT approved_by, approval_reason FROM posting_reviews WHERE id = 'posting-review:retry'"
            ).fetchone()
        self.assertEqual(saved, ("user:vi", "Confirmed the deadline on the application page"))
        retried = self.run_radar((source,), deadlines={verified.source_url: verified},
                                 now=NOW + timedelta(minutes=3))
        self.assertEqual(retried.deadlines[0][1].status, "created")
        self.assertEqual(len(self.calendar.events_by_key), 1)
        self.assertEqual(len(self.telegram.messages_by_key), 1)


if __name__ == "__main__":
    unittest.main()
