import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from keeper_core.adapters import ModelAnswer, SearchHit
from keeper_core.contracts import Eligibility
from keeper_core.db import migrate
from keeper_core.fakes import FakeCalendar, FakeTavily, FakeTelegram
from keeper_core.posting_reviews import (
    get_posting_review, list_posting_reviews, load_posting_evidence,
    parse_posting_evidence, preview_posting_review, reviewed_requirements,
    save_posting_review,
)
from keeper_core.radar import build_queries, load_profile, run_radar
from keeper_core.radar_delivery import run_daily_radar
from keeper_core.contracts import ModelMetadata
from keeper_core.radar_enrichment import VerifiedPostingRequirements
from keeper_core.time import to_storage


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "demo" / "profile.md"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
URL = "https://jobs.lever.co/acme/123"
ALIAS = "https://jobs.lever.co/acme/old-123"


def payload(**changes):
    result = {
        "opportunity_id": "opp:1",
        "source_url": URL,
        "source_excerpt": "2027 Computer Science graduates, United States. Apply by 2026-11-20T17:00:00Z.",
        "requirements": {
            "complete": True,
            "graduation_years": [2027],
            "majors": ["Computer Science"],
            "locations": ["United States"],
        },
        "deadline_timestamp": "2026-11-20T17:00:00Z",
    }
    result.update(changes)
    return result


class PostingReviewsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "keeper.db"
        migrate(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.profile = load_profile(PROFILE)
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source, discovered_at, eligibility)
               VALUES ('opp:1', 'key:1', ?, 'Acme', 'Software Engineer Intern', 'jobs.lever.co', ?, 'unverified')""",
            (URL, to_storage(NOW)),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def evidence(self, data=None):
        return parse_posting_evidence(data or payload())

    def record_alias(self):
        self.conn.execute(
            """INSERT INTO opportunity_observations
               (opportunity_id, run_id, observed_url, observed_at, source, eligibility)
               VALUES ('opp:1', 'run:old', ?, ?, 'jobs.lever.co', 'unverified')""",
            (ALIAS, to_storage(NOW)),
        )
        self.conn.commit()

    def alias_evidence(self):
        data = payload(source_url=ALIAS)
        return self.evidence(data)

    def test_parse_constructs_verified_types_and_allows_no_deadline(self):
        evidence = self.evidence(payload(deadline_timestamp=None))
        self.assertEqual(evidence.requirements.graduation_years, (2027,))
        self.assertEqual(evidence.requirements.majors, ("Computer Science",))
        self.assertIsNone(evidence.deadline)
        self.assertEqual(self.evidence().deadline.deadline_at,
                         datetime(2026, 11, 20, 17, tzinfo=timezone.utc))

    def test_deadline_with_nonzero_explicit_offset_normalizes_to_utc(self):
        data = payload(
            source_excerpt="2027 Computer Science graduates. Apply by 2026-11-20T09:00:00-08:00.",
            deadline_timestamp="2026-11-20T09:00:00-08:00",
        )
        evidence = self.evidence(data)
        self.assertEqual(evidence.deadline.raw_timestamp, "2026-11-20T09:00:00-08:00")
        self.assertEqual(evidence.deadline.deadline_at,
                         datetime(2026, 11, 20, 17, tzinfo=timezone.utc))

    def test_load_rejects_duplicate_keys_unknown_fields_and_oversize_files(self):
        path = Path(self.tmp.name) / "review.json"
        path.write_text('{"opportunity_id":"a","opportunity_id":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            load_posting_evidence(path)
        with self.assertRaisesRegex(ValueError, "unsupported evidence fields"):
            self.evidence({**payload(), "approved_by": "user:1"})
        path.write_bytes(b" " * (64 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            load_posting_evidence(path)

    def test_parse_enforces_types_timezone_and_excerpt_limit(self):
        for changed in (
            {"requirements": {"complete": 1}},
            {"requirements": {"complete": True, "graduation_years": [True]}},
            {"deadline_timestamp": "2026-11-20T09:00:00"},
            {"source_excerpt": "é" * 5000},
        ):
            data = payload()
            data.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.evidence(data)

    def test_preview_accepts_canonical_or_recorded_alias_without_writes(self):
        preview = preview_posting_review(self.conn, self.profile, self.evidence())
        self.assertEqual(preview.eligibility, Eligibility.ELIGIBLE)
        self.record_alias()
        alias = preview_posting_review(self.conn, self.profile, self.alias_evidence())
        self.assertEqual(alias.eligibility, Eligibility.ELIGIBLE)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM posting_reviews").fetchone()[0], 0)

    def test_preview_rejects_an_unrecorded_source_page(self):
        data = payload(source_url="https://jobs.lever.co/acme/not-recorded")
        with self.assertRaisesRegex(ValueError, "not recorded"):
            preview_posting_review(self.conn, self.profile, self.evidence(data))

    def test_partial_and_conflicting_requirements_keep_existing_eligibility_rules(self):
        partial = payload(requirements={"complete": False, "graduation_years": [2027]})
        conflict = payload(requirements={"complete": True, "graduation_years": [2028]})
        self.assertEqual(preview_posting_review(self.conn, self.profile, self.evidence(partial)).eligibility,
                         Eligibility.UNVERIFIED)
        self.assertEqual(preview_posting_review(self.conn, self.profile, self.evidence(conflict)).eligibility,
                         Eligibility.INELIGIBLE)

    def test_save_persists_evidence_updates_current_state_and_replays_original_time(self):
        evidence = self.evidence()
        saved = save_posting_review(self.conn, self.profile, evidence, review_id="review:1",
                                    reviewed_by="user:vi", now=NOW)
        self.conn.commit()
        self.assertEqual(saved["eligibility"], Eligibility.ELIGIBLE)
        self.assertEqual(saved["reviewed_at"], NOW)
        self.assertEqual(self.conn.execute(
            "SELECT eligibility FROM opportunities WHERE id = 'opp:1'").fetchone()[0], "eligible")
        replay = save_posting_review(self.conn, self.profile, evidence, review_id="review:1",
                                     reviewed_by="user:vi", now=NOW.replace(day=29))
        self.assertEqual(replay["reviewed_at"], NOW)
        self.assertEqual(len(list_posting_reviews(self.conn, "opp:1")), 1)

    def test_reused_review_id_conflicts_on_evidence_or_reviewer(self):
        evidence = self.evidence()
        save_posting_review(self.conn, self.profile, evidence, review_id="review:1",
                            reviewed_by="user:vi", now=NOW)
        with self.assertRaisesRegex(ValueError, "different evidence"):
            save_posting_review(self.conn, self.profile, self.evidence(payload(deadline_timestamp=None)),
                                review_id="review:1", reviewed_by="user:vi", now=NOW)
        with self.assertRaisesRegex(ValueError, "different evidence"):
            save_posting_review(self.conn, self.profile, evidence, review_id="review:1",
                                reviewed_by="user:other", now=NOW)

    def test_backdated_review_is_rejected_and_tied_reviews_use_row_order(self):
        first = self.evidence()
        save_posting_review(self.conn, self.profile, first, review_id="review:b",
                            reviewed_by="user:vi", now=NOW)
        older = self.evidence(payload(requirements={"complete": False}))
        with self.assertRaisesRegex(ValueError, "predate"):
            save_posting_review(self.conn, self.profile, older, review_id="review:c",
                                reviewed_by="user:vi", now=NOW.replace(day=27))
        tie = self.evidence(payload(requirements={"complete": False}))
        save_posting_review(self.conn, self.profile, tie, review_id="review:a",
                            reviewed_by="user:vi", now=NOW)
        self.assertEqual(self.conn.execute(
            "SELECT eligibility FROM opportunities WHERE id = 'opp:1'").fetchone()[0], "unverified")
        self.assertEqual([item["id"] for item in list_posting_reviews(self.conn)], ["review:b", "review:a"])

    def test_get_list_and_latest_requirements_return_persisted_review_data(self):
        evidence = self.evidence()
        save_posting_review(self.conn, self.profile, evidence, review_id="review:1",
                            reviewed_by="user:vi", now=NOW)
        self.conn.commit()
        record = get_posting_review(self.conn, "review:1")
        self.assertEqual(record["source_excerpt"], evidence.source_excerpt)
        self.assertEqual(record["deadline_timestamp"], "2026-11-20T17:00:00Z")
        self.assertIsNone(record["approved_at"])
        self.assertEqual(get_posting_review(self.conn, "missing"), None)
        reused = reviewed_requirements(self.conn)
        self.assertEqual(set(reused), {URL})
        self.assertEqual(reused[URL].graduation_years, (2027,))

    def test_recorded_alias_review_applies_to_canonical_rediscovery_and_digest_retry(self):
        self.record_alias()
        alias_evidence = self.alias_evidence()
        save_posting_review(self.conn, self.profile, alias_evidence, review_id="review:alias",
                            reviewed_by="user:vi", now=NOW)
        self.conn.commit()
        requirements = reviewed_requirements(self.conn)
        self.assertIs(requirements[URL], requirements[ALIAS])
        self.assertEqual(requirements[URL].source_url, ALIAS)

        hit = SearchHit(URL, "Software Engineer Intern at Acme", "Build developer tools.",
                        "jobs.lever.co")
        query_results = {query: (hit,) for query in build_queries(self.profile, max_queries=1)}
        tavily = FakeTavily(query_results)
        model_id = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
        from keeper_core.fakes import FakeModel
        model = FakeModel({"radar": ModelAnswer(
            json.dumps({"score": 8, "evidence": "Software Engineer Intern",
                        "reason": "Direct target role match"}),
            ModelMetadata(model_id, model_id, "posting-review-test"),
        )})
        telegram = FakeTelegram()
        first = run_daily_radar(
            self.db, self.profile, tavily, model, telegram, recipient_reference="review-test",
            run_id="rediscovery:canonical", now=NOW.replace(minute=1),
            calendar=FakeCalendar(), max_queries=1,
        )
        retry = run_daily_radar(
            self.db, self.profile, tavily, model, telegram, recipient_reference="review-test",
            run_id="rediscovery:canonical", now=NOW.replace(minute=2),
            calendar=FakeCalendar(), max_queries=1,
        )
        self.assertEqual(first.radar.results.opportunities[0].eligibility, Eligibility.ELIGIBLE)
        self.assertEqual(first.digest.status, "sent")
        self.assertEqual(retry.digest.message_id, first.digest.message_id)
        self.assertEqual(len(telegram.messages_by_key), 1)

    def test_unrecorded_requirements_source_cannot_apply_to_another_posting_url(self):
        hit = SearchHit(URL, "Software Engineer Intern at Acme", "Build developer tools.",
                        "jobs.lever.co")
        query = build_queries(self.profile, max_queries=1)[0]

        class Searcher:
            def search(self, requested, *, limit=5):
                if requested != query:
                    raise AssertionError("unexpected search query")
                return (hit,)

        class Model:
            def __init__(self):
                self.calls = 0

            def complete(self, task_type, prompt):
                self.calls += 1
                raise AssertionError("must validate source before scoring")

        requirements = VerifiedPostingRequirements(
            "https://jobs.lever.co/acme/unrecorded", "requirements copied from another page", True,
            graduation_years=(2027,),
        )
        model = Model()
        with self.assertRaisesRegex(ValueError, "not recorded"):
            run_radar(self.profile, Searcher(), model, self.conn, run_id="bad-source",
                      discovered_at=NOW, verified_requirements={URL: requirements}, max_queries=1)
        self.assertEqual(model.calls, 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM opportunity_observations WHERE run_id = 'bad-source'"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
