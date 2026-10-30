"""The demo story fixtures drive the tracker, the calendar and drafts as the video needs.

Offline: the triage decisions come from the fixtures' gold labels, so this checks the email
wording against Keeper's rules (career, scam, lifecycle signals, deadline cues, tracked
identity), not the live model.
"""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from actions.__main__ import act
from actions.base import ALREADY_DONE, DONE, SKIPPED, ActionContext
from keeper_core import application_cli
from keeper_core.adapters import ModelAnswer
from keeper_core.application_messages import detect_application_signal
from keeper_core.applications import list_applications
from keeper_core.contracts import EmailRecord, IngestionMetadata, ModelMetadata, TriageBucket, TriageResult
from keeper_core.deadlines import source_deadline_candidates
from keeper_core.fakes import FakeCalendar, FakeGmail, FakeModel
from keeper_core.workflow import stable_key
from triage.rules import CAREER, scam_signals
from triage.store import ResultStore, saved_decisions

FIXTURES = Path(__file__).resolve().parents[1] / "demo" / "fixtures"
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)  # demo day
ACCOUNT = "demo@example.test"
TRACK = ["add", "--organization", "Example Inc", "--title", "Software Engineer Intern",
         "--url", "https://careers.example.test/jobs/software-engineer-intern",
         "--found-at", "2026-10-26T12:00:00Z"]  # before the first story email


def story(part):
    return json.loads((FIXTURES / f"story_part{part}.json").read_text(encoding="utf-8"))["emails"]


def record(item):
    # Gmail's snippet is what Keeper stores and what the tracker reads; these bodies fit in it.
    at = datetime.fromisoformat(item["received_at"].replace("Z", "+00:00"))
    return EmailRecord(f"gmail:{item['id']}", "gmail", item["id"], item["thread_id"], item["sender"],
                       item["subject"], at, item["body_text"], ("INBOX",), None,
                       IngestionMetadata(at, "story", "gmail-inbox"))


def decision(email, gold):
    deadline = datetime.fromisoformat(gold["deadline"].replace("Z", "+00:00")) if gold["deadline"] else None
    return TriageResult(
        id=stable_key("triage", email.id), email_id=email.id, bucket=TriageBucket(gold["bucket"]),
        reason="gold label", career=gold["career"], scam_signals=tuple(gold["scam_signals"]),
        extracted_deadline=deadline, confidence=None, model=None, classified_at=NOW,
        deadline_evidence=gold["deadline_evidence"],
    )


class StoryWordingTest(unittest.TestCase):
    def test_each_email_trips_exactly_the_rules_it_is_meant_to(self):
        for item in story(1) + story(2):
            with self.subTest(item["id"]):
                email, gold = record(item), item["gold"]
                self.assertLessEqual(len(item["body_text"]), 160)  # whole body inside Gmail's snippet
                self.assertIn("Example Inc", item["body_text"])
                self.assertIn("Software Engineer Intern", item["body_text"])
                self.assertTrue(CAREER.search(f"{item['subject']} {item['body_text']}"))
                self.assertEqual(set(scam_signals(email, ())), set(gold["scam_signals"]))
                signal = detect_application_signal(email)
                expected = gold["application_transition"]
                self.assertEqual(signal.signal.status.value if signal.signal else None, expected)
                deadlines = source_deadline_candidates(email.id, email.subject, email.body_excerpt, None, now=NOW)
                self.assertEqual([c.deadline_at for c in deadlines],
                                 [decision(email, gold).extracted_deadline] if gold["deadline"] else [])
                self.assertFalse([c.flags for c in deadlines if c.flags])


class StoryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "keeper.db"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(application_cli.main(["--db", str(self.db)] + TRACK), 0)
        self.store = ResultStore(self.db, account=ACCOUNT)
        self.gmail, self.calendar = FakeGmail(()), FakeCalendar()

    def tearDown(self):
        self.tmp.cleanup()

    def run_part(self, part):
        for item in story(part):
            email = record(item)
            self.store.save(email, decision(email, item["gold"]))
        pairs = saved_decisions(self.db)
        self.gmail = FakeGmail(tuple(email for email, _ in pairs))
        self.gmail.drafts_by_key = getattr(self, "drafts", {})
        model = FakeModel({"draft": ModelAnswer("Thanks, I'll reply with times shortly.",
                                                ModelMetadata("super", "super", "t"))})
        ctx = ActionContext(self.db, self.gmail, self.calendar, model, lambda: NOW, dry_run=False)
        outcomes = {(o.step, o.email_id.removeprefix("gmail:")): (o.status, o.detail) for o in act(pairs, ctx)}
        self.drafts = self.gmail.drafts_by_key
        return outcomes

    def status(self):
        with closing(sqlite3.connect(self.db)) as conn:
            [app] = list_applications(conn)
        return app.status.value

    def test_part_one_then_part_two(self):
        first = self.run_part(1)
        self.assertEqual(first[("tracker", "story-01")][0], DONE)
        self.assertIn("now applied", first[("tracker", "story-01")][1])
        self.assertIn("now interview", first[("tracker", "story-02")][1])
        self.assertEqual(first[("tracker", "story-03")][0], SKIPPED)  # a deadline isn't a status change
        self.assertEqual(first[("deadlines", "story-03")],
                         (DONE, "deadline created: 2026-11-06T17:00:00.000000Z"))
        self.assertEqual((first[("drafts", "story-01")][0], first[("drafts", "story-02")][0]), (DONE, DONE))
        self.assertEqual(self.status(), "interview")
        self.assertEqual(len(self.calendar.events_by_key), 1)

        second = self.run_part(2)
        self.assertEqual(second[("deadlines", "story-04")],
                         (DONE, "deadline updated: 2026-11-13T17:00:00.000000Z"))
        self.assertEqual(len(self.calendar.events_by_key), 1)  # the extension moved the one event
        self.assertFalse([key for key in second if key[1] == "story-05"])  # the scam gets no action
        for key in (("tracker", "story-01"), ("tracker", "story-02"), ("deadlines", "story-03"),
                    ("drafts", "story-02")):
            self.assertEqual(second[key][0], ALREADY_DONE, key)
        self.assertEqual(self.status(), "interview")

    def test_found_at_must_be_utc_and_not_in_the_future(self):
        for value in ("2026-10-26T12:00:00", "2026-10-26T12:00:00-07:00", "next week", "2999-01-01T00:00:00Z"):
            with self.subTest(value), contextlib.redirect_stderr(io.StringIO()):
                args = ["--db", str(self.db)] + TRACK[:-1] + [value]
                try:
                    code = application_cli.main(args)
                except SystemExit as exc:  # argparse rejects a malformed time
                    code = exc.code
                self.assertEqual(code, 2)

    def test_tracking_twice_keeps_one_application(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(application_cli.main(["--db", str(self.db)] + TRACK), 0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(len(list_applications(conn)), 1)


if __name__ == "__main__":
    unittest.main()
