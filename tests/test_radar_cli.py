import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from keeper_core.adapters import AuthenticationError, ProviderUnavailable
from keeper_core.fakes import FakeTelegram
from keeper_core.radar import load_profile
from keeper_core.radar_cli import main


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "demo" / "profile.md"


class RadarCliTest(unittest.TestCase):
    def adapters(self):
        from demo.radar_lifecycle import offline_adapters
        return offline_adapters(load_profile(PROFILE))

    def verified_requirements(self):
        from demo.radar_lifecycle import HIT
        from keeper_core.radar_enrichment import VerifiedPostingRequirements
        return {HIT.url: VerifiedPostingRequirements(
            HIT.url, "The full synthetic requirements section has no restrictions.", complete=True)}

    def test_packaged_launcher_works_outside_checkout_without_private_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            shutil.copytree(ROOT / "keeper_core", app / "keeper_core",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (app / "demo").mkdir()
            for name in ("profile.md", "radar_lifecycle.py", "reviewed_posting.json", "reviewed_radar_lifecycle.py"):
                shutil.copy2(ROOT / "demo" / name, app / "demo" / name)
            data = root / "private-data"
            completed = subprocess.run(
                ["bash", str(ROOT / "agent" / "bin" / "keeper"), "radar", "run"],
                cwd=root, text=True, capture_output=True, check=True,
                env={"PATH": os.defpath, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1",
                     "KEEPER_APP_DIR": str(app), "KEEPER_PYTHON": sys.executable,
                     "KEEPER_DATA_DIR": str(data), "HERMES_HOME": str(root / "hermes")},
            )
            report = json.loads(completed.stdout)
            self.assertEqual((report["mode"], report["source"]), ("preview", "synthetic"))
            self.assertEqual(len(report["opportunities"]), 1)
            self.assertEqual(report["digest"]["status"], "empty")
            self.assertIsNone(report["digest"]["message_id"])
            self.assertFalse(data.exists())
            scenario = subprocess.run(
                [sys.executable, "-m", "demo.reviewed_radar_lifecycle"], cwd=root,
                env={"PATH": os.defpath, "PYTHONPATH": str(app), "PYTHONDONTWRITEBYTECODE": "1"},
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(json.loads(scenario.stdout)["counts"]["calendar_events"], 1)

    def test_default_is_synthetic_preview_and_never_touches_requested_db(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "production.db"
            original = b"leave this database alone"
            db.write_bytes(original)
            with patch("keeper_core.radar_cli._live_adapters") as live_adapters, \
                    patch("keeper_core.radar_cli._live_telegram") as live_telegram, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["run", "--db", str(db)]), 0)
            live_adapters.assert_not_called()
            live_telegram.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertEqual((report["mode"], report["source"], report["scoring"]),
                             ("preview", "synthetic", "simulated"))
            self.assertEqual(report["digest"]["status"], "empty")
            self.assertIsNone(report["digest"]["message_id"])
            self.assertEqual(len(report["opportunities"]), 1)
            self.assertEqual(db.read_bytes(), original)

    def test_apply_requires_live_and_recipient_before_profile_access(self):
        with patch("keeper_core.radar_cli._profile_path", side_effect=AssertionError("read profile")):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["run", "--apply"]), 2)
            self.assertIn("--apply requires --live", errors.getvalue())
            with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": ""}):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main(["run", "--live", "--apply"]), 2)
                self.assertIn("--recipient or TELEGRAM_CHAT_ID", errors.getvalue())

    def test_missing_nano_key_prevents_tavily_construction(self):
        settings = SimpleNamespace(api_key="", models={})
        config = types.ModuleType("router.config")
        config.load_settings = Mock(return_value=settings)
        router = types.ModuleType("router")
        router.__path__ = []
        with patch.dict(sys.modules, {"router": router, "router.config": config}), \
                patch("integrations.tavily.LiveTavily.from_key_file") as tavily_factory:
            with self.assertRaises(AuthenticationError):
                from keeper_core.radar_cli import _live_adapters
                _live_adapters(Path("/private/secrets"))
        config.load_settings.assert_called_once_with()
        tavily_factory.assert_not_called()

    def test_apply_persists_and_replay_does_not_send_twice(self):
        tavily, model = self.adapters()
        telegram = FakeTelegram()
        with tempfile.TemporaryDirectory() as directory, \
                patch("keeper_core.radar_delivery.reviewed_requirements", return_value=self.verified_requirements()):
            db = Path(directory) / "keeper.db"
            arguments = ["run", "--live", "--apply", "--profile", str(PROFILE),
                         "--db", str(db), "--recipient", "test-chat", "--run-id", "cli:one"]
            with patch("keeper_core.radar_cli._live_adapters", return_value=(tavily, model)), \
                    patch("keeper_core.radar_cli._live_telegram", return_value=telegram), \
                    contextlib.redirect_stdout(io.StringIO()) as first:
                self.assertEqual(main(arguments), 0)
            first_report = json.loads(first.getvalue())
            self.assertEqual(first_report["mode"], "apply")
            self.assertEqual(first_report["digest"]["status"], "sent")
            self.assertTrue(first_report["digest"]["message_id"])
            self.assertNotIn("test-chat", first.getvalue())

            with patch("keeper_core.radar_cli._live_adapters", return_value=(tavily, model)), \
                    patch("keeper_core.radar_cli._live_telegram", return_value=telegram), \
                    contextlib.redirect_stdout(io.StringIO()) as replay:
                self.assertEqual(main(arguments), 0)
            self.assertEqual(json.loads(replay.getvalue())["digest"]["message_id"],
                             first_report["digest"]["message_id"])
            self.assertEqual(len(telegram.messages_by_key), 1)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM opportunities").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM opportunity_observations").fetchone()[0], 1)

    def test_live_preview_uses_readonly_database_copy_and_no_live_telegram(self):
        tavily, model = self.adapters()
        telegram = FakeTelegram()
        with tempfile.TemporaryDirectory() as directory, \
                patch("keeper_core.radar_delivery.reviewed_requirements", return_value=self.verified_requirements()):
            db = Path(directory) / "keeper.db"
            apply_args = ["run", "--live", "--apply", "--profile", str(PROFILE), "--db", str(db),
                          "--recipient", "test-chat", "--run-id", "cli:stored"]
            with patch("keeper_core.radar_cli._live_adapters", return_value=(tavily, model)), \
                    patch("keeper_core.radar_cli._live_telegram", return_value=telegram), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(apply_args), 0)
            before = db.read_bytes()
            with patch("keeper_core.radar_cli._live_adapters", return_value=(tavily, model)), \
                    patch("keeper_core.radar_cli._live_telegram", side_effect=AssertionError("delivery")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["run", "--live", "--profile", str(PROFILE), "--db", str(db),
                                       "--run-id", "cli:preview", "--recipient", "test-chat"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["source"], "live")
            self.assertEqual(report["digest"]["status"], "empty")
            self.assertEqual(report["digest"]["message_id"], None)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(len(telegram.messages_by_key), 1)

    def test_live_outage_is_redacted_and_profile_or_budget_errors_precede_adapters(self):
        tavily, model = self.adapters()

        class Outage:
            def search(self, query, *, limit=5):
                raise ProviderUnavailable("private token value")

        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors, \
                patch("keeper_core.radar_cli._live_adapters", return_value=(Outage(), model)), \
                patch("keeper_core.radar_cli._live_telegram", side_effect=AssertionError("delivery")):
            self.assertEqual(main(["run", "--live", "--profile", str(PROFILE)]), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("ProviderUnavailable", errors.getvalue())
        self.assertNotIn("private token value", errors.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.md"
            invalid.write_text("not a profile", encoding="utf-8")
            with patch("keeper_core.radar_cli._offline_adapters") as offline_adapters, \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["run", "--profile", str(invalid)]), 2)
            offline_adapters.assert_not_called()
            self.assertIn("ValueError", errors.getvalue())

        with patch("keeper_core.radar_cli._live_adapters", side_effect=AssertionError("adapter")), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["run", "--live", "--max-queries", "13"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
