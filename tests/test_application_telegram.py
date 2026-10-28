import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from keeper_core.application_cli import main
from keeper_core.application_telegram import format_apps_response
from keeper_core.applications import create_application
from keeper_core.db import migrate


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-26T12:00:00.000000Z"
WHEN = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


class ApplicationTelegramTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keeper.db"
        migrate(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_application(self, index, title, status="found", outcome=None):
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source,
                discovered_at, eligibility)
               VALUES (?, ?, ?, 'Example Inc', ?, 'fixture', ?, 'unverified')""",
            (f"opp:{index}", f"key:{index}", f"https://example.test/{index}", title, STAMP),
        )
        app = create_application(self.conn, f"opp:{index}", created_at=WHEN,
                                 source_evidence=f"https://example.test/{index}")
        if status != "found":
            self.conn.execute(
                "UPDATE applications SET status = ?, decision_outcome = ? WHERE id = ?",
                (status, outcome, app.id),
            )
            self.conn.execute(
                """INSERT INTO application_status_changes
                   (id, application_id, status, changed_at, reason, source_evidence, decision_outcome)
                   VALUES (?, ?, ?, ?, 'Fixture state', 'Fixture evidence', ?)""",
                (f"change:{index}", app.id, status, STAMP, outcome),
            )
        return app

    def test_read_only_response_from_same_database(self):
        self.add_application(1, "Software Engineer Intern", "interview")
        self.add_application(2, "Data Science Intern", "decision", "rejected")
        self.conn.commit()
        before = self.path.stat().st_mtime_ns
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--db", str(self.path), "telegram"])
        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn("Applications (2)", rendered)
        self.assertIn("Software Engineer Intern — interview", rendered)
        self.assertIn("Data Science Intern — decision (rejected)", rendered)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_empty_and_bounded_response(self):
        self.assertEqual(format_apps_response(self.conn), "No applications tracked.")
        for index in range(1, 26):
            self.add_application(index, f"Role {index}")
        text = format_apps_response(self.conn, max_chars=220)
        self.assertLessEqual(len(text), 220)
        self.assertIn("more", text)

    def test_missing_database_stays_missing(self):
        missing = Path(self.tmp.name) / "missing.db"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--db", str(missing), "telegram"])
        self.assertEqual(code, 0)
        self.assertIn("not ready", output.getvalue())
        self.assertFalse(missing.exists())

    def test_gateway_quick_command_dispatch_is_configured(self):
        deploy = (ROOT / "agent/deploy.sh").read_text(encoding="utf-8")
        self.assertIn('"apps": {"type": "exec", "command": "/sandbox/keeper/bin/keeper apps telegram"}', deploy)
        env = dict(os.environ, KEEPER_APP_DIR=str(ROOT), KEEPER_PYTHON=sys.executable)
        proc = subprocess.run(
            [str(ROOT / "agent/bin/keeper"), "apps", "--db", str(self.path), "telegram"],
            cwd=ROOT, env=env, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "No applications tracked.")


if __name__ == "__main__":
    unittest.main()
