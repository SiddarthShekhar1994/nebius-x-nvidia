import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from keeper_core.adapters import ModelAnswer, ProviderUnavailable, SearchHit
from keeper_core.contracts import Eligibility, ModelMetadata
from keeper_core.fakes import FakeCalendar, FakeTelegram, FakeTavily
from keeper_core.radar import build_queries, load_profile
from keeper_core.radar_deadlines import VerifiedPostingDeadline
from keeper_core.radar_delivery import run_daily_radar
from keeper_core.radar_enrichment import VerifiedPostingRequirements
from keeper_core.time import from_storage


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
        return run_daily_radar(
            self.db, self.profile, tavily, self.model, telegram or self.telegram,
            recipient_reference="demo-chat", run_id=run_id, now=now,
            calendar=self.calendar, verified_requirements=requirements,
            verified_deadlines=deadlines, max_queries=1, results_per_query=10,
        )

    def count(self, table):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

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
        self.model = Model({f"Org{i}": 11 - i for i in range(1, 8)})
        outcome = self.run_radar(tuple(hit(i) for i in range(1, 8)))
        self.assertEqual(len(outcome.digest.opportunity_ids), 5)
        self.assertEqual(self.count("opportunities"), 7)
        body = outcome.digest.text
        for i in range(1, 6):
            self.assertIn(f"Org{i}", body)
            self.assertIn(f"{11-i}/10", body)
            self.assertIn(f"https://jobs.lever.co/org{i}/{i}", body)
        self.assertNotIn("Org6", body)
        self.assertNotIn("Org7", body)
        self.assertIn("Matches the target software role", body)
        self.assertLessEqual(len(body), 4096)

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
        self.assertIn("unverified", outcome.digest.text)

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
        self.model = Model({"Org1": 4})
        outcome = self.run_radar((hit(1),), now=NOW + timedelta(minutes=3), telegram=telegram)
        self.assertIn("8/10", outcome.digest.text)
        self.assertEqual(len(telegram.messages_by_key), 1)

    def test_only_verified_page_deadline_reaches_calendar(self):
        raw = "2026-11-01T23:59:00Z"
        source = hit(1, snippet=f"Apply by {raw}")
        first = self.run_radar((source,))
        self.assertEqual(first.deadlines, ())
        self.assertEqual(self.calendar.events_by_key, {})
        verified = VerifiedPostingDeadline(
            "https://jobs.lever.co/org1/1", f"Applications close {raw}", raw,
            datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc),
        )
        second = self.run_radar((source,), deadlines={verified.source_url: verified},
                                now=NOW + timedelta(minutes=2))
        self.assertEqual(second.deadlines[0][1].status, "created")
        self.assertEqual(len(self.calendar.events_by_key), 1)
        self.assertEqual(self.count("deadline_events"), 1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE opportunities SET deadline = NULL, deadline_evidence = NULL")
        same = self.run_radar((source,), deadlines={verified.source_url: verified},
                              now=NOW + timedelta(minutes=3))
        self.assertEqual(same.deadlines[0][1].status, "unchanged")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(from_storage(conn.execute("SELECT deadline FROM opportunities").fetchone()[0]),
                             verified.deadline_at)
        moved_raw = "2026-11-02T23:59:00Z"
        moved = VerifiedPostingDeadline(verified.source_url, f"Applications close {moved_raw}",
                                        moved_raw, datetime(2026, 11, 2, 23, 59, tzinfo=timezone.utc))
        review = self.run_radar((source,), deadlines={moved.source_url: moved},
                                now=NOW + timedelta(minutes=4))
        self.assertEqual(review.deadlines[0][1].status, "review")
        with closing(sqlite3.connect(self.db)) as conn:
            stored = conn.execute("SELECT deadline FROM opportunities").fetchone()[0]
        self.assertEqual(from_storage(stored), verified.deadline_at)
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
        with self.assertRaises(ProviderUnavailable):
            self.run_radar((source,), deadlines={verified.source_url: verified})
        self.assertEqual(self.count("deadline_events"), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT provider_event_id FROM deadline_events").fetchone()[0])
        retried = self.run_radar((source,), deadlines={verified.source_url: verified},
                                 now=NOW + timedelta(minutes=1))
        self.assertEqual(retried.deadlines[0][1].status, "created")
        self.assertEqual(len(self.calendar.events_by_key), 1)
        self.assertEqual(len(self.telegram.messages_by_key), 1)


if __name__ == "__main__":
    unittest.main()
