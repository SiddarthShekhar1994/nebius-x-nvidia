import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.application_cli import main
from keeper_core.applications import apply_application_transition, create_application, get_application
from keeper_core.contracts import ApplicationStatus
from keeper_core.db import migrate


WHEN = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
STAMP = "2026-10-26T12:00:00.000000Z"
ROOT = Path(__file__).resolve().parents[1]


class ApplicationCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keeper.db"
        migrate(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source,
                discovered_at, eligibility)
               VALUES ('opp:1', 'key:1', 'https://example.test/job/1',
                       'Example Inc', 'Software Engineer Intern', 'fixture', ?, 'unverified')""", (STAMP,),
        )
        self.app = create_application(self.conn, "opp:1", created_at=WHEN,
                                      source_evidence="https://example.test/job/1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(["--db", str(self.path), *args])
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_list_history_and_approved_correction_preserve_audit(self):
        code, rows, _ = self.invoke("list")
        self.assertEqual(code, 0)
        self.assertEqual([(row["id"], row["status"], row["organization"]) for row in rows],
                         [(self.app.id, "found", "Example Inc")])
        code, history, _ = self.invoke("history", self.app.id)
        self.assertEqual(code, 0)
        self.assertEqual(history["history"][0]["source_evidence"], "https://example.test/job/1")
        correction_args = (
            "correct", self.app.id, "--status", "decision", "--outcome", "offered",
            "--actor", "user:1", "--reason", "Verified offer by phone",
            "--evidence", "User reviewed recruiter call notes", "--correction-id", "call:1", "--approve",
        )
        code, corrected, _ = self.invoke(*correction_args)
        self.assertEqual(code, 0)
        self.assertEqual((corrected["status"], corrected["decision_outcome"]), ("decision", "offered"))
        code, repeated, _ = self.invoke(*correction_args)
        self.assertEqual(code, 0)
        self.assertEqual(repeated["correction_id"], corrected["correction_id"])
        code, history, _ = self.invoke("history", self.app.id)
        self.assertEqual(len(history["history"]), 2)
        self.assertEqual(history["history"][-1]["correction_id"], "call:1")
        self.assertEqual(history["history"][-1]["source_evidence"], "User reviewed recruiter call notes")
        self.assertEqual(history["history"][-1]["decision_outcome"], "offered")
        self.assertEqual(history["manual_override"]["actor_reference"], "user:1")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM correction_records").fetchone()[0], 1)

    def test_correction_requires_explicit_approval_and_does_not_partially_write(self):
        args = (
            "correct", self.app.id, "--status", "applied", "--actor", "user:1",
            "--reason", "Corrected", "--evidence", "User note", "--correction-id", "fix:1",
        )
        code, _, error = self.invoke(*args)
        self.assertEqual(code, 2)
        self.assertIn("--approve", error)
        self.assertEqual(get_application(self.conn, self.app.id).status, ApplicationStatus.FOUND)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM correction_records").fetchone()[0], 0)
        code, _, error = self.invoke(*args, "--approve", "--outcome", "offered")
        self.assertEqual(code, 2)
        self.assertIn("outcome", error)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM correction_records").fetchone()[0], 0)

    def test_manual_override_blocks_later_automatic_change(self):
        self.invoke("correct", self.app.id, "--status", "found", "--actor", "user:1",
                    "--reason", "Still searching", "--evidence", "User review",
                    "--correction-id", "fix:hold", "--approve")
        self.conn.execute(
            """INSERT INTO email_records
               (id, provider, provider_id, sender, subject, received_at, body_excerpt,
                ingested_at, ingestion_run_id, ingestion_source)
               VALUES ('gmail:1', 'gmail', '1', 'x@example.test', 'Interview', ?, '', ?, 'run', 'fixture')""",
            (STAMP, STAMP),
        )
        before = get_application(self.conn, self.app.id)
        after = apply_application_transition(
            self.conn, self.app.id, ApplicationStatus.INTERVIEW,
            changed_at=WHEN + timedelta(hours=2), reason="Interview invitation",
            source_evidence="Interview invitation", source_email_id="gmail:1",
        )
        self.assertEqual(after, before)
        self.assertEqual(len(after.status_history), 2)

    def test_approved_reversal_preserves_prior_decision_and_reused_id_conflicts(self):
        first = (
            "correct", self.app.id, "--status", "decision", "--outcome", "rejected",
            "--actor", "user:1", "--reason", "Recorded rejection",
            "--evidence", "Recruiter call", "--correction-id", "fix:decision", "--approve",
        )
        self.assertEqual(self.invoke(*first)[0], 0)
        second = (
            "correct", self.app.id, "--status", "found", "--actor", "user:1",
            "--reason", "Recruiter corrected the rejection", "--evidence", "Follow-up call",
            "--correction-id", "fix:reversal", "--approve",
        )
        self.assertEqual(self.invoke(*second)[0], 0)
        code, history, _ = self.invoke("history", self.app.id)
        self.assertEqual(code, 0)
        self.assertEqual(history["status"], "found")
        self.assertIsNone(history["decision_outcome"])
        self.assertEqual([entry["status"] for entry in history["history"]],
                         ["found", "decision", "found"])
        self.assertEqual(history["history"][1]["decision_outcome"], "rejected")
        code, _, error = self.invoke(
            "correct", self.app.id, "--status", "applied", "--actor", "user:1",
            "--reason", "Changed details", "--evidence", "Follow-up call",
            "--correction-id", "fix:reversal", "--approve",
        )
        self.assertEqual(code, 2)
        self.assertIn("different details", error)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM correction_records").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM application_status_changes").fetchone()[0], 3)

    def test_dispatch_through_keeper_shell(self):
        env = dict(os.environ, KEEPER_APP_DIR=str(ROOT), KEEPER_PYTHON=sys.executable)
        proc = subprocess.run([str(ROOT / "agent/bin/keeper"), "apps", "--db", str(self.path), "list"],
                              env=env, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)[0]["id"], self.app.id)


if __name__ == "__main__":
    unittest.main()
