"""Phase 4 state, source verification, replay, and command acceptance tests."""
import base64
import contextlib
import io
import json
import os
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from integrations.gmail import LiveGmail
from keeper_core.adapters import AuthenticationError, IdempotencyConflict, ProviderUnavailable
from keeper_core.db import migrate
from keeper_core.ingest import insert_email_once
from keeper_core.contracts import TriageBucket
from tests.test_gmail_live import Service
from tests.test_triage import FakeModel, UTC_TIME, email, payload
from triage.classifier import classify_email
from triage.preview import load_emails
from triage.sources import FixtureSource, GmailSource
from triage.store import ResultStore
from triage.run import run_triage
from triage.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "demo/fixtures/inbox_75.json"


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keeper.db"
        self.record = email()
        self.result = classify_email(self.record, FakeModel(payload()), classified_at=UTC_TIME)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_read_does_not_create_state(self):
        self.assertIsNone(ResultStore(self.path).get(self.record.id))
        self.assertFalse(self.path.exists())

    def test_save_roundtrip_replay_and_conflict_preserve_original(self):
        store = ResultStore(self.path, account="demo@example.test")
        store.save(self.record, self.result)
        self.assertEqual(ResultStore(self.path, account="demo@example.test").get(self.record.id), self.result)
        store.save(self.record, self.result)
        with self.assertRaises(IdempotencyConflict):
            store.save(self.record, replace(self.result, bucket=TriageBucket.DUE))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM email_records").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM triage_results").fetchone()[0], 1)

    def test_account_binding_prevents_cross_account_replay(self):
        ResultStore(self.path, account="demo@example.test").save(self.record, self.result)
        with self.assertRaises(IdempotencyConflict):
            ResultStore(self.path, account="other@example.test").get(self.record.id)

    def test_saved_fixture_rejects_changed_source_content(self):
        source = load_emails(FIXTURE)[0]
        result = classify_email(source, FakeModel(payload()), classified_at=UTC_TIME)
        store = ResultStore(self.path)
        store.save(source, result)
        with self.assertRaises(IdempotencyConflict):
            store.get(source.id, email=replace(source, body_excerpt="Changed content"))

    def test_phase3_ingested_email_is_reused_without_identity_conflict(self):
        migrate(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            insert_email_once(conn, self.record)
        store = ResultStore(self.path, account="demo@example.test")
        store.save(self.record, self.result)
        self.assertEqual(store.get(self.record.id), self.result)

    def test_version_two_upgrade_preserves_ingested_email(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for name in ("001_initial.sql", "002_ingestion_outcomes.sql"):
                conn.executescript((ROOT / "keeper_core/migrations" / name).read_text())
            conn.execute("PRAGMA user_version=2")
            insert_email_once(conn, self.record)
        store = ResultStore(self.path, account="demo@example.test")
        self.assertIsNone(store.get(self.record.id))
        store.save(self.record, self.result)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT count(*) FROM email_records").fetchone()[0], 1)

    def test_gmail_save_requires_account_and_unbound_legacy_decision_is_rejected(self):
        with self.assertRaises(ValueError):
            ResultStore(self.path).save(self.record, self.result)
        store = ResultStore(self.path, account="demo@example.test")
        store.save(self.record, self.result)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DELETE FROM triage_account")
        with self.assertRaises(IdempotencyConflict):
            store.get(self.record.id)

    def test_result_identity_and_transaction_rollback(self):
        store = ResultStore(self.path, account="demo@example.test")
        with self.assertRaises(ValueError):
            store.save(self.record, replace(self.result, email_id="gmail:wrong"))
        self.assertFalse(self.path.exists())
        migrate(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_triage BEFORE INSERT ON triage_results BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.save(self.record, self.result)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM email_records").fetchone()[0], 0)

    def test_approved_user_correction_blocks_automatic_replay(self):
        store = ResultStore(self.path, account="demo@example.test")
        store.save(self.record, self.result)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO correction_records(id,target_type,target_id,correction,created_at,approval_status) "
                "VALUES('correction:1','triage',?,'Keep this','2026-10-27T12:00:00Z','approved')",
                (self.result.id,),
            )
        with self.assertRaises(IdempotencyConflict):
            store.get(self.record.id)


class GmailRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keeper.db"
        self.manifest = Path(self.tmp.name) / "seed.json"
        self.api = Service()
        self.gmail = LiveGmail(self.api, run_id="triage", max_attempts=1)
        self.digest = "a" * 64
        self.entries = {
            f"demo-email-{n:03d}": {
                "status": "stored", "provider_id": str(n),
                "message_id_header": f"<demo-email-{n:03d}.{self.digest}@keeper-demo.example.test>",
            } for n in (1, 2, 3)
        }
        for n, entry in enumerate(self.entries.values(), 1):
            self.api.message_map[str(n)]["payload"]["headers"][-1]["value"] = entry["message_id_header"]
        self.write_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def write_manifest(self, **changes):
        self.manifest.write_text(json.dumps({
            "version": 1, "account": "demo@example.test", "fixture_sha256": self.digest,
            "messages": self.entries, **changes,
        }))

    def source(self):
        return GmailSource.from_manifest(self.gmail, self.manifest, "demo@example.test")

    def test_dry_run_has_no_product_writes_or_gmail_mutations(self):
        result = run_triage(self.source(), FakeModel(payload()), ResultStore(self.path, account="demo@example.test"),
                            classified_at=UTC_TIME)
        self.assertEqual(result["summary"]["total"], 3)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.api.label_map, {})
        self.assertEqual(result["results"][0]["fixture_id"], "demo-email-001")

    def test_wrong_account_or_manifest_never_writes(self):
        with self.assertRaises(AuthenticationError):
            self.gmail.verify_account("someone-else@example.test")
        self.write_manifest(account="other@example.test")
        with self.assertRaises(ValueError):
            self.source()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.api.label_map, {})

    def test_pending_duplicate_and_non_synthetic_manifest_entries_rejected(self):
        self.entries["demo-email-001"]["status"] = "pending"
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.source()
        self.entries["demo-email-001"]["status"] = "stored"
        self.entries["demo-email-001"]["provider_id"] = "2"
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.source()
        self.entries["demo-email-001"]["provider_id"] = "1"
        self.entries["demo-email-001"]["message_id_header"] = "<unrelated@example.test>"
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.source()

    def test_source_identity_failure_is_visible_and_other_messages_continue(self):
        self.api.message_map["1"]["payload"]["headers"][-1]["value"] = "<wrong>"
        result = run_triage(self.source(), FakeModel(payload()), ResultStore(self.path, account="demo@example.test"),
                            save=True, apply=True, classified_at=UTC_TIME)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["summary"]["applied"], 2)
        self.assertEqual(self.api.message_map["1"]["labelIds"], ["INBOX"])

    def test_apply_replays_saved_decisions_after_restart_and_preserves_inbox(self):
        model = FakeModel(payload(bucket="junk", career=False))
        store = ResultStore(self.path, account="demo@example.test")
        first = run_triage(self.source(), model, store, save=True, apply=True, classified_at=UTC_TIME)
        self.assertEqual(first["summary"]["applied"], 3)
        self.assertEqual(len(model.calls), 3)
        self.gmail = LiveGmail(self.api, run_id="restart")
        second = run_triage(self.source(), FakeModel({}), store, save=True, apply=True, classified_at=UTC_TIME)
        self.assertEqual(second["summary"]["reused"], 3)
        self.assertEqual(len([name for name, _ in self.api.calls if name == "messages.modify"]), 3)
        self.assertTrue(all("INBOX" in item["labelIds"] for item in self.api.message_map.values()))
        self.assertTrue(all(name.startswith("KEEPER_") for name in self.api.label_map))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM triage_results").fetchone()[0], 3)

    def test_label_failure_retains_plan_for_retry_without_model_recall(self):
        self.api.failures["messages.modify"] = [TimeoutError("private transport detail")]
        store = ResultStore(self.path, account="demo@example.test")
        result = run_triage(self.source(), FakeModel(payload()), store, save=True, apply=True, classified_at=UTC_TIME)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertIsNotNone(store.get("gmail:1"))
        self.assertNotIn("private transport detail", json.dumps(result))
        rerun = FakeModel({})
        result = run_triage(self.source(), rerun, store, save=True, apply=True, classified_at=UTC_TIME)
        self.assertEqual(rerun.calls, [])
        self.assertEqual(result["summary"]["failed"], 0)

    def test_failed_save_never_applies_labels(self):
        store = ResultStore(self.path, account="demo@example.test")
        with patch.object(ResultStore, "save", side_effect=sqlite3.OperationalError("private db path")):
            result = run_triage(self.source(), FakeModel(payload()), store, save=True, apply=True,
                                classified_at=UTC_TIME)
        self.assertEqual(result["summary"]["failed"], 3)
        self.assertEqual(self.api.label_map, {})
        self.assertNotIn("private db path", json.dumps(result))

    def test_full_message_body_drives_classification(self):
        body = "Ordinary text. " * 80 + "Software internship opportunity."
        self.api.message_map["1"]["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(body.encode()).decode()
        result = run_triage(self.source(), FakeModel(payload(bucket="junk", career=False)),
                            ResultStore(self.path, account="demo@example.test"), limit=1, classified_at=UTC_TIME)
        self.assertEqual(result["results"][0]["bucket"], "actionable")

    def test_transient_inference_failure_is_not_saved_and_can_recover(self):
        class Broken:
            def complete(self, *args):
                raise ProviderUnavailable("private error")
        store = ResultStore(self.path, account="demo@example.test")
        failed = run_triage(self.source(), Broken(), store, save=True, apply=True, limit=1,
                            require_live=True, classified_at=UTC_TIME)
        self.assertEqual(failed["summary"]["failed"], 1)
        self.assertIsNone(store.get("gmail:1"))
        self.assertEqual(self.api.label_map, {})
        recovered = run_triage(self.source(), FakeModel(payload()), store, save=True, apply=True,
                               limit=1, require_live=True, classified_at=UTC_TIME)
        self.assertEqual(recovered["summary"]["applied"], 1)

    def test_fatal_error_preserves_completed_work_and_stops_further_calls(self):
        class Expiring(FakeModel):
            def complete(self, task, prompt):
                if self.calls:
                    raise AuthenticationError("private token")
                return super().complete(task, prompt)
        result = run_triage(self.source(), Expiring(payload()),
                            ResultStore(self.path, account="demo@example.test"),
                            save=True, apply=True, classified_at=UTC_TIME)
        self.assertEqual(result["fatal_error"], "AuthenticationError")
        self.assertEqual(result["summary"]["applied"], 1)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["summary"]["unprocessed"], 1)
        self.assertEqual(result["errors"][0]["fixture_id"], "demo-email-002")
        self.assertNotIn("private token", json.dumps(result))

    def test_cli_gmail_apply_with_injected_providers_and_fatal_return_code(self):
        arguments = ["run", "--gmail", "--live", "--apply", "--token", "/private/runtime.json",
                     "--manifest", str(self.manifest), "--account", "demo@example.test", "--db", str(self.path)]
        with patch("triage.credentials.gmail_from_token", return_value=self.gmail), \
                patch("triage.__main__.RouterModel", return_value=FakeModel(payload())), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(arguments), 0)
        self.assertEqual(json.loads(output.getvalue())["summary"]["applied"], 3)
        with patch("triage.credentials.gmail_from_token", side_effect=AuthenticationError("private")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(arguments), 2)
        self.assertNotIn("private", errors.getvalue())

    def test_overlong_body_requires_review(self):
        body = "x" * 20001
        self.api.message_map["1"]["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(body.encode()).decode()
        model = FakeModel(payload(bucket="junk", career=False))
        result = run_triage(self.source(), model, ResultStore(self.path, account="demo@example.test"),
                            limit=1, classified_at=UTC_TIME)
        self.assertEqual(model.calls, [])
        self.assertIsNone(result["results"][0]["bucket"])


class RunCommandTest(unittest.TestCase):
    def test_all_75_fixture_records_save_replay_and_compare_offline(self):
        from triage.evaluate import compare_results
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "offline.db"
            arguments = ["run", "--save", "--db", str(path)]
            with contextlib.redirect_stdout(io.StringIO()) as first:
                self.assertEqual(main(arguments), 0)
            with contextlib.redirect_stdout(io.StringIO()) as second:
                self.assertEqual(main(arguments), 0)
            original, replay = json.loads(first.getvalue()), json.loads(second.getvalue())
            self.assertEqual(original["summary"]["total"], 75)
            self.assertEqual(replay["summary"]["reused"], 75)
            self.assertEqual(replay["results"], [
                {**row, "reused": True} for row in original["results"]
            ])
            comparison = compare_results(FIXTURE, replay)
            self.assertEqual(comparison["evaluated"], 75)
            self.assertEqual(comparison["review_count"], 75)
            self.assertFalse(comparison["measured_model_quality"])
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM triage_results").fetchone()[0], 75)

    def test_run_is_dry_by_default_and_save_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keeper.db"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["run", "--db", str(path), "--limit", "2"]), 0)
            self.assertEqual(json.loads(output.getvalue())["operation"], "dry-run")
            self.assertFalse(path.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--db", str(path), "--limit", "2", "--save"]), 0)
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM triage_results").fetchone()[0], 2)

    def test_live_run_rejects_saved_offline_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keeper.db"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", "--db", str(path), "--limit", "1", "--save"]), 0)
            with patch("triage.__main__.RouterModel", return_value=FakeModel(payload())), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["run", "--db", str(path), "--limit", "1", "--live"]), 1)
            self.assertEqual(json.loads(output.getvalue())["summary"]["failed"], 1)

    def test_compare_command_is_read_only_and_rejects_invalid_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["run", "--db", str(Path(directory) / "absent.db"), "--limit", "1"]), 0)
            path.write_text(output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as scored:
                self.assertEqual(main(["compare", "--report", str(path)]), 0)
            self.assertFalse(json.loads(scored.getvalue())["measured_model_quality"])
            path.write_text("{}")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["compare", "--report", str(path)]), 2)

    def test_compare_reads_reports_saved_by_windows_powershell(self):
        # Windows PowerShell's ">" writes UTF-16 with a byte order mark.
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["run", "--db", str(Path(directory) / "absent.db"), "--limit", "1"]), 0)
            for encoding in ("utf-16", "utf-8-sig", "utf-8"):
                path = Path(directory) / f"report-{encoding}.json"
                path.write_text(output.getvalue(), encoding=encoding)
                with self.subTest(encoding=encoding), contextlib.redirect_stdout(io.StringIO()) as scored:
                    self.assertEqual(main(["compare", "--report", str(path)]), 0)
                    self.assertEqual(json.loads(scored.getvalue())["evaluated"], 1)

    def test_fixture_rejects_gmail_options_and_database_failure_returns_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["run", "--account", "demo@example.test"])
        with patch("triage.__main__.ResultStore", side_effect=sqlite3.OperationalError("private path")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["run"]), 2)
        self.assertNotIn("private path", errors.getvalue())

    def test_apply_requires_live_verified_gmail_source_before_any_setup(self):
        for args in [["run", "--apply"], ["run", "--gmail", "--apply"],
                     ["run", "--gmail", "--live"], ["run", "--gmail", "--live", "--account", "demo@example.test"]]:
            with contextlib.redirect_stderr(io.StringIO()), patch("triage.__main__.RouterModel") as model:
                with self.assertRaises(SystemExit):
                    main(args)
                model.assert_not_called()

    @unittest.skipIf(os.name == "nt", "runs the bash launcher; WSL or Linux only")
    def test_launcher_dispatches_triage_without_sandbox_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "KEEPER_APP_DIR": str(ROOT), "KEEPER_PYTHON": sys.executable,
                           "KEEPER_DATA_DIR": directory, "HERMES_HOME": directory}
            result = subprocess.run(["bash", str(ROOT / "agent/bin/keeper"), "triage", "run", "--limit", "1"],
                                    cwd=directory, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["summary"]["total"], 1)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
