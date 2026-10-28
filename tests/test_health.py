import json
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock

from integrations.gateway_access import check_gateway_access
from integrations.health import check_local, main
from keeper_core.db import migrate


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hermes = self.root / ".hermes"
        self.hermes.mkdir(mode=0o700)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.db = self.data / "keeper.db"
        migrate(self.db)
        self.db.chmod(0o600)
        self.runtime = self.hermes / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.state = self.runtime / "state.db"
        sqlite3.connect(self.state).close()
        self.state.chmod(0o600)
        self.config = self.hermes / "config.yaml"
        self.config.write_text("teach: keeper skills teach\nkeep: keeper skills keep\napps: keeper apps telegram\n")
        self.config.chmod(0o600)
        self.env = self.hermes / ".env"
        self.env.write_text("TEST_PLACEHOLDER=yes\n")
        self.env.chmod(0o600)
        self.skills = self.hermes / "skills" / "keeper"
        self.skills.mkdir(parents=True, mode=0o700)
        self.soul = self.runtime / "SOUL.md"
        self.soul.write_text("Keeper")
        self.soul.chmod(0o600)
        self.key = self.secrets / "tavily.key"
        self.key.write_text("fictional-key")
        self.key.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX file modes; secrets checks run in the Linux sandbox")
    def test_local_check_and_private_key_mode(self):
        check_local("tavily", self.secrets, self.db)
        self.key.chmod(0o644)
        with self.assertRaises(PermissionError):
            check_local("tavily", self.secrets, self.db)

    @unittest.skipIf(os.name == "nt", "POSIX file modes; secrets checks run in the Linux sandbox")
    def test_google_scope_check_and_database_readiness(self):
        token = self.secrets / "google-token.json"
        token.write_text(json.dumps({"scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar.events"]}))
        token.chmod(0o600)
        check_local("gmail", self.secrets, self.db)
        check_local("calendar", self.secrets, self.db)
        with self.assertRaises(FileNotFoundError):
            check_local("calendar", self.secrets, self.root / "missing.db")

    @unittest.skipIf(os.name == "nt", "POSIX users and file modes; the probe runs in the Linux sandbox")
    def test_gateway_probe_uses_temporary_scoped_writes(self):
        with mock.patch.dict(os.environ, {"KEEPER_HERMES_STATE_DB": str(self.state)}):
            result = check_gateway_access(self.hermes, self.data, secrets_dir=self.secrets,
                                          soul_files=(self.soul,))
        self.assertTrue(all(value.startswith("PASS") for value in result.values()), result)
        self.assertEqual(self.soul.read_text(), "Keeper")
        self.assertFalse(list(self.skills.glob(".keeper-health-*")))

    def test_gateway_probe_rejects_broad_writes(self):
        self.skills.chmod(0o777)
        with mock.patch.dict(os.environ, {"KEEPER_HERMES_STATE_DB": str(self.state)}):
            result = check_gateway_access(self.hermes, self.data, secrets_dir=self.secrets,
                                          soul_files=(self.soul,))
        self.assertTrue(result["learned skill write for /keep"].startswith("FAIL"))

    def test_gateway_probe_rejects_missing_quick_command_and_public_secret(self):
        self.config.write_text("teach: keeper skills teach\n")
        self.key.chmod(0o644)
        with mock.patch.dict(os.environ, {"KEEPER_HERMES_STATE_DB": str(self.state)}):
            result = check_gateway_access(self.hermes, self.data, secrets_dir=self.secrets,
                                          soul_files=(self.soul,))
        self.assertTrue(result["gateway config and quick commands"].startswith("FAIL"))
        self.assertTrue(result["secrets least privilege"].startswith("FAIL"))

    def test_cli_returns_nonzero_for_missing_credentials(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["adapters", "--provider", "telegram", "--secrets-dir",
                                   str(self.secrets), "--db", str(self.db)]), 1)
        self.assertIn("telegram: FAIL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
