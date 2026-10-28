import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from keeper_core.adapters import SearchHit
from keeper_core.contracts import Eligibility
from keeper_core.radar import build_queries, load_profile, normalize_hits, search_opportunities


PROFILE = Path(__file__).resolve().parents[1] / "demo" / "profile.md"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(PROFILE)

    def test_profile_and_bounded_queries(self):
        self.assertEqual(self.profile.graduation_year, 2027)
        self.assertFalse(self.profile.authorization_supplied)
        queries = build_queries(self.profile, max_queries=3)
        self.assertEqual(len(queries), 3)
        self.assertTrue(all(len(query) <= 180 for query in queries))
        self.assertTrue(all("Software" in query or "Machine" in query for query in queries))
        self.assertTrue(all("authorization" not in query.lower() for query in queries))
        self.assertTrue(any("AI infrastructure" in query for query in build_queries(self.profile)))

    def test_profile_rejects_unsupported_authorization_statement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profile.md"
            path.write_text(PROFILE.read_text().replace("- User statement: Not specified",
                                                        "- User statement: Authorized"))
            with self.assertRaisesRegex(ValueError, "work authorization"):
                load_profile(path)

    def test_query_strips_user_search_operators(self):
        changed = replace(self.profile, roles=('Software Engineer site:bad.test "secret"',))
        query = build_queries(changed, max_queries=1)[0]
        self.assertNotIn(":", query)
        self.assertNotIn('"', query)

    def test_primary_source_normalization_and_duplicate_filter(self):
        hits = (
            SearchHit("https://www.linkedin.com/jobs/view/123", "Software Engineer Intern at Acme", "Apply now", "linkedin.com"),
            SearchHit("https://jobs.lever.co/acme/123?utm_source=radar", "Software Engineer Intern at Acme", "Apply now", "jobs.lever.co"),
            SearchHit("https://jobs.lever.co/acme/123#top", "Software Engineer Intern at Acme", "Apply now", "jobs.lever.co"),
            SearchHit("https://careers.acme.test/jobs/other", "Software Engineer Intern at Acme", "Apply now", "careers.acme.test"),
        )
        result = normalize_hits(self.profile, hits, discovered_at=NOW)
        self.assertEqual(len(result.opportunities), 1)
        item = result.opportunities[0]
        self.assertEqual(item.canonical_url, "https://jobs.lever.co/acme/123")
        self.assertEqual((item.title, item.organization, item.source),
                         ("Software Engineer Intern", "Acme", "jobs.lever.co"))
        self.assertEqual(item.discovered_at, NOW)
        self.assertEqual(item.eligibility, Eligibility.UNVERIFIED)
        self.assertEqual(len(result.skipped), 3)
        self.assertTrue(any("duplicate" in skip.reason for skip in result.skipped))
        self.assertTrue(any("aggregator" in skip.reason for skip in result.skipped))

    def test_filters_closed_obvious_mismatch_and_unverified_claim(self):
        hits = (
            SearchHit("https://careers.acme.test/jobs/1", "Marketing Intern at Acme", "Apply", "x"),
            SearchHit("https://careers.acme.test/jobs/2", "Senior Software Engineer at Acme", "Apply", "x"),
            SearchHit("https://careers.acme.test/jobs/3", "Software Engineer Intern at Acme", "Applications are closed", "x"),
            SearchHit("https://careers.beta.test/jobs/4", "Machine Learning Intern at Beta", "US citizens only", "x"),
            SearchHit("https://careers.gamma.test/jobs/5", "Software Engineer Intern", "Apply", "x"),
            SearchHit("https://random-blog.test/post/6", "Software Engineer Intern at Delta", "Apply", "x"),
        )
        result = normalize_hits(self.profile, hits, discovered_at=NOW)
        self.assertEqual(len(result.opportunities), 1)
        self.assertIn("verify on the application page", result.opportunities[0].eligibility_reason)
        self.assertEqual(result.opportunities[0].eligibility, Eligibility.UNVERIFIED)
        self.assertEqual(len(result.skipped), 5)
        self.assertTrue(any("closed" in skip.reason for skip in result.skipped))
        self.assertTrue(any("senior" in skip.reason for skip in result.skipped))
        self.assertTrue(any("target roles" in skip.reason for skip in result.skipped))
        self.assertTrue(any("cannot be verified" in skip.reason for skip in result.skipped))
        self.assertTrue(any("source not verified" in skip.reason for skip in result.skipped))

    def test_search_caps_adapter_calls(self):
        class Searcher:
            def __init__(self):
                self.calls = []

            def search(self, query, *, limit=5):
                self.calls.append((query, limit))
                return ()

        searcher = Searcher()
        result = search_opportunities(self.profile, searcher, discovered_at=NOW,
                                      max_queries=2, results_per_query=3)
        self.assertEqual(len(searcher.calls), 2)
        self.assertEqual([limit for _, limit in searcher.calls], [3, 3])
        self.assertEqual(result.opportunities, ())
        self.assertEqual(result.queries, tuple(query for query, _ in searcher.calls))


if __name__ == "__main__":
    unittest.main()
