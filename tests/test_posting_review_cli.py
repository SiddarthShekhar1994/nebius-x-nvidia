import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from demo.radar_lifecycle import HIT, offline_adapters
from keeper_core.fakes import FakeCalendar, FakeTelegram
from keeper_core.radar import load_profile
from keeper_core.radar_cli import main
from keeper_core.radar_delivery import run_daily_radar


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "demo" / "profile.md"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
RAW_DEADLINE = "2099-11-20T17:00:00Z"


def evidence(*, requirements=None, deadline=RAW_DEADLINE):
    return {
        "opportunity_id": "radar-opportunity:expected",
        "source_url": HIT.url,
        "source_excerpt": (
            "2027 graduates in Computer Science, United States. "
            f"Applications close {deadline}."
        ),
        "requirements": requirements or {
            "complete": True,
            "graduation_years": [2027],
            "majors": ["Computer Science"],
            "locations": ["United States"],
        },
        "deadline_timestamp": deadline,
    }


class PostingReviewCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.db = self.directory / "keeper.db"
        self.profile = load_profile(PROFILE)
        tavily, model = offline_adapters(self.profile)
        run_daily_radar(
            self.db, self.profile, tavily, model, FakeTelegram(),
            recipient_reference="synthetic-seed", run_id="discovery:1", now=NOW,
        )
        self.payload = evidence()
        with closing(sqlite3.connect(self.db)) as conn:
            self.payload["opportunity_id"] = conn.execute(
                "SELECT id FROM opportunities"
            ).fetchone()[0]
        self.evidence_file = self.directory / "review.json"
        self.write_evidence()

    def tearDown(self):
        self.tmp.cleanup()

    def write_evidence(self, value=None):
        self.evidence_file.write_text(json.dumps(value or self.payload), encoding="utf-8")

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = main(args)
        return code, output.getvalue(), errors.getvalue()

    def review_args(self, review_id="review:1"):
        return ["review", str(self.evidence_file), "--review-id", review_id,
                "--reviewer", "vi", "--profile", str(PROFILE), "--db", str(self.db)]

    def test_discovery_review_approval_calendar_and_digest_retry(self):
        before = self.db.read_bytes()
        with patch("keeper_core.radar_cli._live_calendar", side_effect=AssertionError("calendar")), \
                patch("keeper_core.radar_cli._live_telegram", side_effect=AssertionError("telegram")):
            code, output, errors = self.invoke(self.review_args())
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["review"]["eligibility"], "eligible")
        self.assertEqual(self.db.read_bytes(), before)

        with patch("keeper_core.radar_cli._live_calendar", side_effect=AssertionError("calendar")), \
                patch("keeper_core.radar_cli._live_telegram", side_effect=AssertionError("telegram")):
            code, output, errors = self.invoke(self.review_args() + ["--apply"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["mode"], "applied")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute(
                "SELECT eligibility FROM opportunities"
            ).fetchone()[0], "eligible")
            self.assertEqual(conn.execute("SELECT count(*) FROM posting_reviews").fetchone()[0], 1)

        before_schedule_preview = self.db.read_bytes()
        calendar = FakeCalendar()
        with patch("keeper_core.radar_cli._live_calendar", side_effect=AssertionError("live adapter")):
            code, output, errors = self.invoke(["schedule", "review:1", "--db", str(self.db)])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["calendar"]["action"], "create")
        self.assertEqual(calendar.events_by_key, {})
        self.assertEqual(self.db.read_bytes(), before_schedule_preview)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deadline_events").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT approved_by FROM posting_reviews WHERE id = 'review:1'"
            ).fetchone()[0], None)

        with patch("keeper_core.radar_cli._live_calendar", return_value=calendar):
            code, output, errors = self.invoke([
                "schedule", "review:1", "--db", str(self.db), "--live", "--apply",
                "--approve", "--reviewer", "vi", "--reason", "Checked application page",
            ])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["calendar"]["status"], "created")
        self.assertEqual(len(calendar.events_by_key), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute(
                "SELECT approved_by, approval_reason FROM posting_reviews WHERE id = 'review:1'"
            ).fetchone(), ("vi", "Checked application page"))
            self.assertEqual(conn.execute("SELECT count(*) FROM deadline_events").fetchone()[0], 1)

        before_digest_preview = self.db.read_bytes()
        with patch("keeper_core.radar_cli._live_telegram", side_effect=AssertionError("live adapter")):
            code, output, errors = self.invoke([
                "digest", "--db", str(self.db), "--recipient", "demo-chat", "--run-id", "digest:one",
            ])
        self.assertEqual((code, errors), (0, ""))
        preview = json.loads(output)
        self.assertEqual(preview["digest"]["status"], "preview")
        self.assertIsNone(preview["digest"]["message_id"])
        self.assertEqual(self.db.read_bytes(), before_digest_preview)

        telegram = FakeTelegram()
        arguments = ["digest", "--db", str(self.db), "--recipient", "demo-chat",
                     "--run-id", "digest:one", "--live", "--apply"]
        for _ in range(2):
            with patch("keeper_core.radar_cli._live_telegram", return_value=telegram):
                code, output, errors = self.invoke(arguments)
            self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["digest"]["message_id"], "fake-message:1")
        self.assertEqual(len(telegram.messages_by_key), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM radar_digest_snapshots").fetchone()[0], 1)
            self.assertNotIn("fake-message", " ".join(
                str(value) for row in conn.execute("SELECT * FROM radar_digest_snapshots") for value in row
            ))

    def test_review_history_is_read_only_and_rejects_malformed_payload_without_echo(self):
        bad = evidence()
        bad["approval_reason"] = "secret source payload marker"
        self.write_evidence(bad)
        before = self.db.read_bytes()
        code, output, errors = self.invoke(self.review_args())
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn("secret source payload marker", errors)
        self.assertEqual(self.db.read_bytes(), before)

        self.write_evidence()
        self.invoke(self.review_args() + ["--apply"])
        before = self.db.read_bytes()
        code, output, errors = self.invoke(["reviews", "--db", str(self.db),
                                           "--opportunity", self.payload["opportunity_id"]])
        self.assertEqual((code, errors), (0, ""))
        records = json.loads(output)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "review:1")
        self.assertIsNone(records[0]["approved_by"])
        self.assertEqual(self.db.read_bytes(), before)

    def test_partial_review_declined_review_and_stale_review_cannot_schedule(self):
        partial = dict(self.payload)
        partial["requirements"] = {"complete": False, "graduation_years": [2027]}
        self.write_evidence(partial)
        code, output, errors = self.invoke(self.review_args("review:partial") + ["--apply"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["review"]["eligibility"], "unverified")
        code, _, errors = self.invoke(["schedule", "review:partial", "--db", str(self.db)])
        self.assertEqual(code, 2)
        self.assertIn("DeadlineReviewRequired", errors)

        self.write_evidence()
        code, output, errors = self.invoke(self.review_args("review:eligible") + ["--apply"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["review"]["eligibility"], "eligible")
        code, _, errors = self.invoke(["schedule", "review:partial", "--db", str(self.db)])
        self.assertEqual(code, 2)
        self.assertIn("DeadlineReviewRequired", errors)

        declined = dict(self.payload)
        declined["requirements"] = {"complete": True, "graduation_years": [2028]}
        self.write_evidence(declined)
        code, output, errors = self.invoke(self.review_args("review:declined") + ["--apply"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["review"]["eligibility"], "ineligible")
        calendar = FakeCalendar()
        with patch("keeper_core.radar_cli._live_calendar", return_value=calendar):
            code, _, errors = self.invoke([
                "schedule", "review:declined", "--db", str(self.db), "--live", "--apply",
                "--approve", "--reviewer", "vi", "--reason", "Try declined posting",
            ])
        self.assertEqual(code, 2)
        self.assertEqual(calendar.events_by_key, {})
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM deadline_events").fetchone()[0], 0)
            self.assertIsNone(conn.execute(
                "SELECT approved_by FROM posting_reviews WHERE id = 'review:declined'"
            ).fetchone()[0])

    def test_missing_live_apply_gates_precede_database_and_adapter_access(self):
        missing_db = self.directory / "missing.db"
        with patch("keeper_core.radar_cli._read_connection", side_effect=AssertionError("database")), \
                patch("keeper_core.radar_cli._live_calendar", side_effect=AssertionError("calendar")), \
                patch("keeper_core.radar_cli._live_telegram", side_effect=AssertionError("telegram")):
            code, output, errors = self.invoke([
                "schedule", "review:missing", "--db", str(missing_db), "--apply",
            ])
            self.assertEqual(code, 2)
            self.assertIn("requires --live --apply --approve", errors)
            code, output, errors = self.invoke([
                "digest", "--db", str(missing_db), "--apply", "--recipient", "demo-chat",
            ])
            self.assertEqual(code, 2)
            self.assertIn("requires --live --apply", errors)
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
