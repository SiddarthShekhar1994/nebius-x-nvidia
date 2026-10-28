import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.db import MIGRATIONS, migrate
from keeper_core.time import for_display, from_storage, to_storage


STAMP = "2026-10-26T12:00:00.000000Z"


class KeeperDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "nested" / "keeper.db"
        self.assertEqual(migrate(self.db), 7)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def email(self, record_id="gmail:1", provider_id="1", provider="gmail"):
        self.conn.execute(
            """INSERT INTO email_records
               (id, provider, provider_id, sender, subject, received_at,
                body_excerpt, ingested_at, ingestion_run_id, ingestion_source)
               VALUES (?, ?, ?, 'recruiter@example.test', 'Interview', ?, '', ?, 'run:1', 'fixture')""",
            (record_id, provider, provider_id, STAMP, STAMP),
        )

    def opportunity(self, record_id="opp:1", url="https://example.test/one", key="one"):
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source,
                discovered_at, eligibility)
               VALUES (?, ?, ?, 'Example', 'Intern', 'fixture', ?, 'unverified')""",
            (record_id, key, url, STAMP),
        )

    def test_bootstrap_is_idempotent_and_keeps_data(self):
        self.email()
        self.conn.commit()
        self.assertEqual(migrate(self.db), 7)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM email_records").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_version_three_history_gets_evidence_and_outcome_on_upgrade(self):
        legacy = Path(self.tmp.name) / "v3.db"
        migrations = Path(self.tmp.name) / "v3_migrations"
        migrations.mkdir()
        for number in (1, 2, 3):
            source = next(MIGRATIONS.glob(f"{number:03d}_*.sql"))
            (migrations / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(migrate(legacy, migrations_dir=migrations), 3)
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """INSERT INTO opportunities
                   (id, dedupe_key, canonical_url, organization, title, source,
                    discovered_at, eligibility)
                   VALUES ('opp', 'key', 'https://example.test/opp', 'Example', 'Intern',
                           'fixture', ?, 'unverified')""", (STAMP,),
            )
            conn.execute(
                """INSERT INTO applications
                   (id, opportunity_id, status, created_at, updated_at, decision_outcome)
                   VALUES ('app', 'opp', 'decision', ?, ?, 'rejected')""", (STAMP, STAMP),
            )
            conn.execute(
                """INSERT INTO application_status_changes
                   (id, application_id, status, changed_at, reason)
                   VALUES ('change', 'app', 'decision', ?, 'Rejection email')""", (STAMP,),
            )
        self.assertEqual(migrate(legacy), 7)
        with closing(sqlite3.connect(legacy)) as conn:
            self.assertEqual(conn.execute(
                "SELECT source_evidence, decision_outcome FROM application_status_changes"
            ).fetchone(), ("Rejection email", "rejected"))

    def test_new_history_requires_source_evidence(self):
        self.opportunity()
        self.conn.execute(
            """INSERT INTO applications (id, opportunity_id, status, created_at, updated_at)
               VALUES ('app:1', 'opp:1', 'found', ?, ?)""", (STAMP, STAMP),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO application_status_changes
                   (id, application_id, status, changed_at, reason)
                   VALUES ('change:1', 'app:1', 'found', ?, 'Discovered')""", (STAMP,),
            )

    def test_unique_email_provider_mapping_and_id(self):
        self.email()
        with self.assertRaises(sqlite3.IntegrityError):
            self.email("gmail:duplicate", "1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.email("gmail:1", "2")
        self.email("other:1", "1", "other")

    def test_unique_canonical_opportunity_and_dedupe_key(self):
        self.opportunity()
        with self.assertRaises(sqlite3.IntegrityError):
            self.opportunity("opp:2", "https://example.test/one", "two")
        with self.assertRaises(sqlite3.IntegrityError):
            self.opportunity("opp:3", "https://example.test/three", "one")

    def test_unknown_dates_and_foreign_keys(self):
        self.email()
        self.opportunity()
        self.conn.execute(
            """INSERT INTO triage_results
               (id, email_id, reason, classified_at)
               VALUES ('triage:1', 'gmail:1', 'Needs review', ?)""",
            (STAMP,),
        )
        self.assertEqual(
            self.conn.execute("SELECT bucket, extracted_deadline FROM triage_results").fetchone(),
            (None, None),
        )
        self.assertIsNone(self.conn.execute("SELECT deadline FROM opportunities").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO application_source_emails(application_id, email_id) VALUES ('missing', 'gmail:1')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO triage_results(id, email_id, reason, classified_at) VALUES ('bad', 'missing', 'x', ?)",
                (STAMP,),
            )

    def test_schema_rejects_invalid_bucket_and_non_utc_timestamp(self):
        self.email()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO triage_results(id, email_id, bucket, reason, classified_at) VALUES ('bad', 'gmail:1', 'spam', 'x', ?)",
                (STAMP,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO triage_results(id, email_id, reason, classified_at) VALUES ('bad', 'gmail:1', 'x', ?)",
                ("2026-10-26T12:00:00+02:00",),
            )
        self.conn.execute(
            "INSERT INTO triage_results(id, email_id, reason, classified_at) VALUES ('good', 'gmail:1', 'x', ?)",
            (STAMP,),
        )

    def test_rejects_database_from_newer_code(self):
        self.conn.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ValueError, "newer"):
            migrate(self.db)

    def test_detects_early_version_one_timestamp_checks(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.execute("CREATE TABLE old_email (received_at TEXT CHECK (received_at LIKE '*Z'))")
            conn.execute("PRAGMA user_version = 1")
        with self.assertRaisesRegex(ValueError, "older version 1 timestamp checks"):
            migrate(legacy)

    def test_failed_migration_does_not_advance_version_or_leave_tables(self):
        directory = Path(self.tmp.name) / "migrations"
        directory.mkdir()
        (directory / "001_initial.sql").write_text(
            (MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (directory / "002_ingestion_outcomes.sql").write_text(
            (MIGRATIONS / "002_ingestion_outcomes.sql").read_text(), encoding="utf-8"
        )
        (directory / "003_triage_account.sql").write_text(
            (MIGRATIONS / "003_triage_account.sql").read_text(), encoding="utf-8"
        )
        (directory / "004_application_evidence.sql").write_text(
            (MIGRATIONS / "004_application_evidence.sql").read_text(), encoding="utf-8"
        )
        (directory / "005_application_activity.sql").write_text(
            (MIGRATIONS / "005_application_activity.sql").read_text(), encoding="utf-8"
        )
        (directory / "006_radar_observations.sql").write_text(
            (MIGRATIONS / "006_radar_observations.sql").read_text(), encoding="utf-8"
        )
        (directory / "007_radar_digest_snapshots.sql").write_text(
            (MIGRATIONS / "007_radar_digest_snapshots.sql").read_text(), encoding="utf-8"
        )
        (directory / "008_broken.sql").write_text(
            "CREATE TABLE should_rollback (id TEXT);\nINVALID SQL;\n", encoding="utf-8"
        )
        with self.assertRaises(sqlite3.Error):
            migrate(self.db, migrations_dir=directory)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'should_rollback'"
        ).fetchone())

    def test_standalone_bootstrap_script(self):
        path = Path(self.tmp.name) / "script.db"
        script = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_keeper_db.py"
        result = subprocess.run(
            [sys.executable, str(script), "--db", str(path)],
            cwd=self.tmp.name, text=True, capture_output=True, check=True,
        )
        self.assertIn("schema version 7", result.stdout)
        with closing(sqlite3.connect(path)) as conn, conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)


class TimestampTest(unittest.TestCase):
    def test_round_trip_and_timezone_display(self):
        incoming = datetime(2026, 10, 26, 5, tzinfo=timezone(timedelta(hours=-7)))
        stored = to_storage(incoming)
        self.assertEqual(stored, STAMP)
        self.assertEqual(from_storage(stored), datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
        self.assertEqual(for_display(from_storage(stored), "America/Los_Angeles"),
                         "2026-10-26 05:00 PDT (-0700)")

    def test_unknown_and_naive_dates(self):
        self.assertIsNone(to_storage(None))
        self.assertIsNone(from_storage(None))
        self.assertEqual(for_display(None, "America/Los_Angeles"), "Unknown")
        with self.assertRaises(ValueError):
            to_storage(datetime(2026, 10, 26, 12))
        with self.assertRaises(ValueError):
            for_display(datetime(2026, 10, 26, 12), "America/Los_Angeles")
        with self.assertRaises(ValueError):
            from_storage("2026-10-26T12:00:00+02:00")


if __name__ == "__main__":
    unittest.main()
