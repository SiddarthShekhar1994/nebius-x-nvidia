import json
import sqlite3
import sys
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from keeper_core.adapters import InvalidProviderResponse, ModelAnswer, SearchHit
from keeper_core.contracts import Eligibility, ModelMetadata
from keeper_core.db import migrate
from keeper_core.ingest import IdentityConflict
from keeper_core.radar import build_queries, load_profile, normalize_hits, run_radar
from keeper_core.radar_enrichment import VerifiedPostingRequirements, check_eligibility, score_opportunity
from keeper_core.radar_store import opportunity_history, persist_radar_run
from keeper_core.workflow import opportunity_key
from integrations.radar_model import RadarRouterModel


PROFILE = Path(__file__).resolve().parents[1] / "demo" / "profile.md"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
HIT = SearchHit("https://jobs.lever.co/acme/123", "Software Engineer Intern at Acme",
                "Build developer tools with the platform team.", "jobs.lever.co")


class Model:
    def __init__(self, payload=None, requested="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"):
        self.payload = payload or {"score": 9, "evidence": "Software Engineer Intern",
                                   "reason": "Direct target role and developer tools match"}
        self.requested = requested
        self.calls = []

    def complete(self, task_type, prompt):
        self.calls.append((task_type, prompt))
        return ModelAnswer(json.dumps(self.payload), ModelMetadata(self.requested, self.requested, "test"))


class RadarEnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(PROFILE)
        self.opportunity = normalize_hits(self.profile, (HIT,), discovered_at=NOW).opportunities[0]

    def test_nano_score_uses_source_phrase_and_one_line_explanation(self):
        model = Model()
        scored = score_opportunity(self.profile, self.opportunity, HIT, model)
        self.assertEqual(scored.score, 9)
        self.assertIn("Software Engineer Intern", scored.explanation)
        self.assertNotIn("\n", scored.explanation)
        self.assertEqual(model.calls[0][0], "radar")
        self.assertNotIn("visa", model.calls[0][1].lower())

    def test_rejects_unsupported_model_output(self):
        cases = (
            Model({"score": 11, "evidence": "Software Engineer Intern", "reason": "Match"}),
            Model({"score": 8, "evidence": "invented phrase", "reason": "Match"}),
            Model({"score": 8, "evidence": "Software Engineer Intern", "reason": "Definitely eligible"}),
            Model(requested="nvidia/nemotron-3-super-120b-a12b"),
        )
        for model in cases:
            with self.subTest(payload=model.payload, requested=model.requested):
                with self.assertRaises(InvalidProviderResponse):
                    score_opportunity(self.profile, self.opportunity, HIT, model)

    def test_router_bridge_requests_radar_without_tools(self):
        requests = []
        model_id = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
        def route(task_type, messages, **kwargs):
            requests.append((task_type, messages, kwargs))
            message = types.SimpleNamespace(content='{"score": 8}', tool_calls=[])
            return types.SimpleNamespace(model=model_id, choices=[
                types.SimpleNamespace(finish_reason="stop", message=message)
            ])
        fake_router = types.ModuleType("router")
        fake_router.route = route
        fake_openai = types.ModuleType("openai")
        fake_openai.APIConnectionError = type("APIConnectionError", (Exception,), {})
        fake_openai.APIStatusError = type("APIStatusError", (Exception,), {})
        settings = types.SimpleNamespace(api_key="synthetic", models={"nano": model_id})
        with patch.dict(sys.modules, {"router": fake_router, "openai": fake_openai}):
            answer = RadarRouterModel(settings).complete("radar", "synthetic opportunity")
        self.assertEqual(answer.metadata.requested_model, model_id)
        self.assertEqual(requests[0][0], "radar")
        self.assertEqual(requests[0][1][0]["role"], "system")
        self.assertNotIn("tools", requests[0][2])

    def test_eligibility_known_conflict_unknown_and_verified_match(self):
        requirements = VerifiedPostingRequirements(
            HIT.url, "2027 graduates in Computer Science; United States; remote allowed", True,
            graduation_years=(2027,), majors=("Computer Science",),
            locations=("United States",), remote_only=True,
        )
        self.assertEqual(check_eligibility(self.profile, self.opportunity, requirements).eligibility,
                         Eligibility.ELIGIBLE)
        year_conflict = replace(requirements, graduation_years=(2028,))
        outcome = check_eligibility(self.profile, self.opportunity, year_conflict)
        self.assertEqual(outcome.eligibility, Eligibility.INELIGIBLE)
        self.assertIn("graduation year", outcome.eligibility_reason)
        unknown = replace(requirements, authorization="us_citizen")
        outcome = check_eligibility(self.profile, self.opportunity, unknown)
        self.assertEqual(outcome.eligibility, Eligibility.UNVERIFIED)
        self.assertIn("not voluntarily supplied", outcome.eligibility_reason)
        self.assertEqual(check_eligibility(self.profile, self.opportunity, None).eligibility,
                         Eligibility.UNVERIFIED)
        city = replace(requirements, locations=("San Francisco",))
        self.assertEqual(check_eligibility(self.profile, self.opportunity, city).eligibility,
                         Eligibility.UNVERIFIED)
        another_country = replace(requirements, locations=("Canada",))
        self.assertEqual(check_eligibility(self.profile, self.opportunity, another_country).eligibility,
                         Eligibility.INELIGIBLE)

    def test_authorization_requires_explicit_statement_and_partial_page_stays_unverified(self):
        profile = replace(self.profile, authorization_supplied=True,
                          authorization_statement="I do not require visa sponsorship")
        requirements = VerifiedPostingRequirements(HIT.url, "No visa sponsorship", True,
                                                   authorization="no_sponsorship")
        self.assertEqual(check_eligibility(profile, self.opportunity, requirements).eligibility,
                         Eligibility.ELIGIBLE)
        profile = replace(profile, authorization_statement="I require visa sponsorship")
        self.assertEqual(check_eligibility(profile, self.opportunity, requirements).eligibility,
                         Eligibility.INELIGIBLE)
        partial = replace(requirements, complete=False, authorization=None)
        self.assertEqual(check_eligibility(profile, self.opportunity, partial).eligibility,
                         Eligibility.UNVERIFIED)
        with self.assertRaisesRegex(ValueError, "source does not match"):
            check_eligibility(profile, self.opportunity,
                              replace(requirements, source_url="https://jobs.lever.co/other/456"))


class RadarHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        path = Path(self.tmp.name) / "keeper.db"
        self.assertEqual(migrate(path), 7)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.profile = load_profile(PROFILE)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_daily_runs_preserve_original_record_and_append_observations(self):
        first = normalize_hits(self.profile, (HIT,), discovered_at=NOW).opportunities[0]
        first = replace(first, score=8, explanation="First score")
        saved = persist_radar_run(self.conn, (first,), run_id="2026-10-28")
        self.assertEqual(saved.new_ids, (first.id,))
        next_hit = replace(HIT, url="https://careers.acme.test/jobs/456")
        second = normalize_hits(self.profile, (next_hit,), discovered_at=NOW + timedelta(days=1)).opportunities[0]
        second = replace(second, score=9, explanation="Second score")
        saved = persist_radar_run(self.conn, (second,), run_id="2026-10-29")
        self.assertEqual(saved.known_ids, (first.id,))
        self.assertEqual(saved.resolved_ids, (first.id,))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM opportunities").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT canonical_url, score FROM opportunities").fetchone(),
                         (first.canonical_url, 9))
        history = opportunity_history(self.conn, first.id)
        self.assertEqual([row[0] for row in history], ["2026-10-28", "2026-10-29"])
        self.assertEqual([row[4] for row in history], [8, 9])
        retry = replace(second, discovered_at=NOW + timedelta(days=1, minutes=3), score=7)
        persist_radar_run(self.conn, (retry,), run_id="2026-10-29")
        self.assertEqual(opportunity_history(self.conn, first.id), history)
        self.assertEqual(self.conn.execute("SELECT score FROM opportunities").fetchone()[0], 9)
        older = replace(second, discovered_at=NOW - timedelta(days=1), score=3)
        persist_radar_run(self.conn, (older,), run_id="2026-10-27")
        self.assertEqual(self.conn.execute("SELECT score FROM opportunities").fetchone()[0], 9)

    def test_identity_conflict_rolls_back_whole_run(self):
        first = normalize_hits(self.profile, (HIT,), discovered_at=NOW).opportunities[0]
        persist_radar_run(self.conn, (first,), run_id="day-one")
        conflict = replace(first, organization="Other",
                           dedupe_key=opportunity_key(first.canonical_url, "Other", first.title))
        with self.assertRaises(IdentityConflict):
            persist_radar_run(self.conn, (conflict,), run_id="day-two")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM opportunity_observations").fetchone()[0], 1)

    def test_end_to_end_run_scores_checks_and_reuses_id(self):
        query = build_queries(self.profile, max_queries=1)[0]

        class Searcher:
            def search(self, requested, *, limit=5):
                self_query = query
                if requested != self_query:
                    raise AssertionError("unexpected query")
                return (HIT,)

        model = Model()
        requirements = VerifiedPostingRequirements(HIT.url, "2027 CS students", True,
                                                   graduation_years=(2027,), majors=("Computer Science",))
        first = run_radar(self.profile, Searcher(), model, self.conn, run_id="day-one",
                          discovered_at=NOW, verified_requirements={HIT.url: requirements}, max_queries=1)
        self.assertEqual(first.results.opportunities[0].score, 9)
        self.assertEqual(first.results.opportunities[0].eligibility, Eligibility.ELIGIBLE)
        self.assertEqual(first.persisted.new_ids, (first.results.opportunities[0].id,))
        second = run_radar(self.profile, Searcher(), model, self.conn, run_id="day-two",
                           discovered_at=NOW + timedelta(days=1), max_queries=1)
        self.assertEqual(second.persisted.known_ids, first.persisted.new_ids)
        self.assertEqual(second.results.opportunities[0].id, first.results.opportunities[0].id)
        self.assertEqual(len(opportunity_history(self.conn, first.persisted.new_ids[0])), 2)


if __name__ == "__main__":
    unittest.main()
