import base64
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

from integrations.gmail import LiveGmail
from integrations.calendar import LiveCalendar
from keeper_core.adapters import GmailAdapter, InvalidProviderResponse, ModelAnswer, RateLimited
from keeper_core.application_cli import _history
from keeper_core.applications import get_application
from keeper_core.calendar_deadlines import sync_deadline_candidate
from keeper_core.contracts import EmailRecord, IngestionMetadata, ModelMetadata, TriageBucket, TriageResult
from keeper_core.db import migrate
from keeper_core.deadlines import extract_deadline_candidates
from keeper_core.ingest import insert_email_once
from keeper_core.reply_drafts import create_reply_draft
from tests.test_calendar_live import Service as CalendarService
from tests.test_gmail_live import Service, gmail_error
from triage.preview import OfflineModel
from triage.run import run_triage
from triage.sources import FixtureSource
from triage.store import ResultStore


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class DraftModel:
    def __init__(self, answer="Thank you for the invitation. I would be glad to discuss the role. Could you share the available times?"):
        self.answer = answer
        self.calls = []

    def complete(self, task_type, prompt):
        self.calls.append((task_type, json.loads(prompt)))
        return ModelAnswer(self.answer, ModelMetadata("nvidia/nemotron-3-super-120b-a12b", None, "test"))


class ReplyDraftTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "keeper.db"
        migrate(self.db)
        self.service = Service()
        self.service.message_map["1"]["payload"]["headers"][1]["value"] = "Interview invitation"
        self.gmail = LiveGmail(self.service, run_id="draft-test", now=lambda: NOW)
        self.model = DraftModel()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            insert_email_once(conn, self.gmail.read_email("gmail:1").record)
            conn.execute("""INSERT INTO opportunities
                (id, dedupe_key, canonical_url, organization, title, source,
                 discovered_at, eligibility)
                VALUES ('opp:1', 'key:1', 'https://example.test/1',
                        'Example', 'Engineer Intern', 'test', ?, 'eligible')""",
                         ("2026-10-28T12:00:00.000000Z",))
            conn.execute("""INSERT INTO applications
                (id, opportunity_id, status, created_at, updated_at)
                VALUES ('app:1', 'opp:1', 'interview', ?, ?)""",
                         ("2026-10-28T12:00:00.000000Z", "2026-10-28T12:00:00.000000Z"))
            conn.execute("INSERT INTO application_source_emails VALUES ('app:1', 'gmail:1')")
        self.service.calls.clear()

    def tearDown(self):
        self.temp.cleanup()

    def draft(self):
        return create_reply_draft(self.db, self.gmail, self.model, "gmail:1",
                                  now=NOW, application_id="app:1")

    def test_context_and_threaded_draft_only(self):
        result = self.draft()
        self.assertFalse(result.reused)
        self.assertEqual(result.subject, "Re: Interview invitation")
        self.assertEqual(self.model.calls[0][0], "draft")
        self.assertEqual(self.model.calls[0][1]["application"],
                         {"organization": "Example", "title": "Engineer Intern"})
        self.assertEqual(self.model.calls[0][1]["message_body"], "Full body")
        raw = self.service.draft_map[result.provider_draft_id]["message"]
        mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw["raw"]))
        self.assertEqual(mime["Subject"], result.subject)
        self.assertEqual(mime["In-Reply-To"], "<1@example.test>")
        self.assertEqual(raw["threadId"], "thread-1")
        self.assertFalse(any(mime.get(name) for name in ("To", "Cc", "Bcc")))
        self.assertFalse(hasattr(self.gmail, "send_email"))
        self.assertFalse(hasattr(GmailAdapter, "send_email"))
        self.assertFalse(hasattr(self.service.messages(), "send"))
        self.assertFalse(hasattr(self.service.drafts(), "send"))
        self.assertTrue(all("send" not in name for name, _ in self.service.calls))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute("SELECT provider_draft_id, requires_review FROM draft_records").fetchone()
        self.assertEqual(row, (result.provider_draft_id, 1))

    def test_repeated_run_and_uncertain_create_reuse_text_and_key(self):
        self.service.failures["drafts.create"] = [gmail_error(429)]
        with self.assertRaises(RateLimited):
            self.draft()
        self.assertEqual(len(self.model.calls), 1)
        first = self.draft()
        second = self.draft()
        self.assertEqual(first.provider_draft_id, second.provider_draft_id)
        self.assertTrue(second.reused)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.service.draft_map), 1)

    def test_invalid_model_output_never_creates_draft(self):
        self.model.answer = "Subject: New topic\nSend this now"
        with self.assertRaises(InvalidProviderResponse):
            self.draft()
        self.assertEqual(self.service.draft_map, {})

    def test_uncertain_identity_missing_context_and_sensitive_request_are_flagged(self):
        self.service.message_map["1"]["payload"]["headers"][0]["value"] = "recruiting@example.test"
        self.service.message_map["1"]["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(
            b"Please send your passport number and bank account details."
        ).decode()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE email_records SET sender = 'recruiting@example.test' WHERE id = 'gmail:1'")
        result = create_reply_draft(self.db, self.gmail, self.model, "gmail:1", now=NOW)
        self.assertEqual(set(result.review_flags), {
            "recruiter_identity_unverified", "application_context_missing", "sensitive_details"})
        self.assertEqual(self.model.calls, [])
        self.assertNotIn("passport", result.body.lower())
        self.assertNotIn("bank account", result.body.lower())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            flags, review = conn.execute(
                "SELECT review_flags_json, requires_review FROM draft_records"
            ).fetchone()
        self.assertEqual(set(json.loads(flags)), set(result.review_flags))
        self.assertEqual(review, 1)

    def test_flagged_draft_is_visible_in_application_history(self):
        self.service.message_map["1"]["payload"]["headers"][0]["value"] = "recruiting@example.test"
        self.service.message_map["1"]["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(
            b"Please send your passport number."
        ).decode()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE email_records SET sender = 'recruiting@example.test' WHERE id = 'gmail:1'")
        result = self.draft()
        self.assertEqual(set(result.review_flags),
                         {"recruiter_identity_unverified", "sensitive_details"})
        self.assertEqual(self.model.calls, [])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            activity = _history(conn, get_application(conn, "app:1"))["activity"]
        self.assertEqual(activity[0]["kind"], "gmail_draft")
        self.assertEqual(set(activity[0]["review_flags"]), set(result.review_flags))

    def test_extension_and_unsent_draft_appear_in_triage_and_application_history(self):
        calendar_service = CalendarService()
        calendar = LiveCalendar(calendar_service, self.db, now=lambda: NOW, sleep=lambda _: None)
        emails = []
        for number, body in (
            (2, "Apply by 2026-11-02T09:30:00Z."),
            (3, "Application deadline extended to 2026-11-05T09:30:00Z."),
        ):
            email = EmailRecord(
                f"gmail:{number}", "gmail", str(number), None,
                "Ada <ada@example.test>", "Application deadline", NOW + timedelta(minutes=number),
                body, (), f"gmail:{number}", IngestionMetadata(NOW, "scenario", "fixture"),
            )
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("PRAGMA foreign_keys = ON")
                insert_email_once(conn, email)
                conn.execute("INSERT INTO application_source_emails VALUES ('app:1', ?)", (email.id,))
            triage = TriageResult(f"triage:{number}", email.id, TriageBucket.DUE,
                                  "Career deadline", True, (), None, None, None, NOW)
            candidate = extract_deadline_candidates(email, triage, now=NOW)[0]
            sync_deadline_candidate(self.db, calendar, candidate, opportunity_id="opp:1",
                                    application_id="app:1", now=NOW)
            emails.append(email)
        draft = self.draft()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            history = _history(conn, get_application(conn, "app:1"))
            event_id, deadline_at = conn.execute(
                "SELECT provider_event_id, deadline_at FROM deadline_events"
            ).fetchone()
        self.assertEqual(deadline_at, "2026-11-05T09:30:00.000000Z")
        self.assertEqual(len(calendar_service.items), 1)
        self.assertEqual(calendar_service.items[event_id]["start"]["dateTime"],
                         "2026-11-05T09:30:00Z")
        self.assertEqual([item["kind"] for item in history["activity"]],
                         ["calendar_created", "calendar_updated", "gmail_draft"])
        self.assertEqual(history["activity"][1]["provider_id"], event_id)
        self.assertEqual(history["activity"][2]["provider_id"], draft.provider_draft_id)
        source = FixtureSource(tuple(emails) + (self.gmail.read_email("gmail:1").record,))
        report = run_triage(source, OfflineModel(), ResultStore(self.db), limit=3)
        self.assertEqual(report["summary"]["calendar_lines"], 2)
        self.assertEqual(report["summary"]["draft_lines"], 1)
        self.assertTrue(all(row["activity_lines"] for row in report["results"]))
        self.assertEqual(len(self.service.draft_map), 1)
        self.assertTrue(all("send" not in name for name, _ in self.service.calls))


if __name__ == "__main__":
    unittest.main()
