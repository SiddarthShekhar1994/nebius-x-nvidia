"""The evidence dashboard renders from missing, empty and real inputs, and escapes everything."""
import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "dashboard" / "generate.py"
spec = importlib.util.spec_from_file_location("keeper_dashboard", MODULE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DashboardReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_inputs(self):
        result = mod.build(None, None, None)
        self.assertIn("No evaluation results JSON supplied", result)
        self.assertIn("Emails ingested", result)
        self.assertIn("<html", result)

    def test_router_db_and_escaping(self):
        db = self.dir / "router.db"
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("CREATE TABLE calls (model TEXT, served_model TEXT, status TEXT, prompt_tokens INTEGER, "
                      "completion_tokens INTEGER, latency_ms INTEGER, cost_usd REAL)")
            c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?)", ("<script>alert(1)</script>", None, "ok", 20, 10, 100, None))
        result = mod.build(None, db, None)
        self.assertIn("&lt;script&gt;", result)
        self.assertNotIn("<script>alert(1)</script>", result)
        self.assertIn("Not recorded", result)

    def test_empty_tables(self):
        db = self.dir / "keeper.db"
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("CREATE TABLE opportunities (eligibility TEXT)")
        self.assertIn("Opportunity eligibility", mod.build(db, None, None))


if __name__ == "__main__":
    unittest.main()
