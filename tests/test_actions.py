"""keeper act: saved triage decisions in; tracker, calendar and draft outcomes out."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from actions import deadlines, drafts, tracker
from actions.__main__ import STEPS, act, main, summarize
from actions.base import ALREADY_DONE, DONE, FAILED, PLANNED, REVIEW, SKIPPED, ActionContext, Outcome
from keeper_core.adapters import AuthenticationError, ModelAnswer, ProviderUnavailable
from keeper_core.application_activity import activity_for_email
from keeper_core.applications import create_application, list_applications
from keeper_core.contracts import (
    EmailRecord, Eligibility, IngestionMetadata, ModelMetadata, Opportunity, TriageBucket, TriageResult,
)
from keeper_core.db import migrate
from keeper_core.fakes import FakeCalendar, FakeGmail, FakeModel
from keeper_core.ingest import insert_opportunity_once
from keeper_core.time import from_storage
from keeper_core.workflow import canonical_url, opportunity_key, stable_key
from triage.preview import load_emails
from triage.store import ResultStore, saved_decisions

FIXTURE = Path(__file__).resolve().parents[1] / "demo/fixtures/inbox_75.json"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
ACCOUNT = "demo@example.test"
ORG, TITLE = "Example Inc", "Software Engineer Intern"


def decision(email, bucket, *, career=True, deadline=None, scam=()):
    return TriageResult(
        id=stable_key("triage", email.id), email_id=email.id, bucket=bucket, reason="test decision",
        career=career, scam_signals=scam, extracted_deadline=deadline, confidence=None, model=None,
        classified_at=NOW, deadline_evidence=deadline.strftime("%Y-%m-%dT%H:%M:%SZ") if deadline else None,
    )


def gmail_email(n, subject, body, hours_ago):
    at = NOW - timedelta(hours=hours_ago)
    return EmailRecord(f"gmail:m{n}", "gmail", f"m{n}", f"thread-{n}", "Dana Lee <dana.lee@example.test>",
                       subject, at, body, ("INBOX",), None, IngestionMetadata(at, "run-1", "gmail-inbox"))


AT = datetime(2026, 11, 5, 17, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 12, 17, tzinfo=timezone.utc)
PAST = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)
SCENARIO = [
    (gmail_email(1, "Application received", f"{ORG} received your application for {TITLE}.", 9), TriageBucket.ACTIONABLE, {}),
    (gmail_email(2, "Interview invitation", f"{ORG} invites you to interview for {TITLE}. Reply with times.", 8),
     TriageBucket.ACTIONABLE, {}),
    (gmail_email(3, "Deadline reminder", f"{ORG}: your {TITLE} application closes at 2026-11-05T17:00:00Z.", 7),
     TriageBucket.DUE, {"deadline": AT}),
    (gmail_email(4, "Deadline extended", f"{ORG}: the {TITLE} deadline has been extended to 2026-11-12T17:00:00Z.", 6),
     TriageBucket.DUE, {"deadline": LATER}),
    (gmail_email(5, "Deadline reminder", "Other Labs: your Data Intern application closes at 2026-11-05T17:00:00Z.", 5),
     TriageBucket.DUE, {"deadline": AT}),
    (gmail_email(6, "Deadline reminder", f"{ORG}: the {TITLE} application closes at 2026-10-20T17:00:00Z.", 4),
     TriageBucket.DUE, {"deadline": PAST}),
    (gmail_email(7, "Claim your internship", "Pay the $50 registration fee today.", 3),
     TriageBucket.ACTIONABLE, {"scam": ("upfront_fee",)}),
]


def scenario_db(directory):
    """A Gmail-bound database with one tracked opportunity and the saved scenario decisions."""
    path = Path(directory) / "keeper.db"
    migrate(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        url = canonical_url("https://example.test/careers/swe-intern")
        insert_opportunity_once(conn, Opportunity("opp:1", opportunity_key(url, ORG, TITLE), url, ORG, TITLE,
                                                  "radar", NOW - timedelta(days=3), 8, "fit", Eligibility.ELIGIBLE,
                                                  None, None))
        create_application(conn, "opp:1", created_at=NOW - timedelta(days=3), source_evidence=url)
    store = ResultStore(path, account=ACCOUNT)
    for email, bucket, extra in SCENARIO:
        store.save(email, decision(email, bucket, **extra))
    return path


def context(path, *, dry_run):
    pairs = saved_decisions(path)
    model = FakeModel({"draft": ModelAnswer("Thanks, I'm available Tuesday.", ModelMetadata("super", "super", "t"))})
    return pairs, ActionContext(path, FakeGmail(tuple(e for e, _ in pairs)), FakeCalendar(), model,
                                lambda: NOW, dry_run=dry_run)


def by_step(outcomes):
    return {(o.step, o.email_id.removeprefix("gmail:")): (o.status, o.detail) for o in outcomes}


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = scenario_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def counts(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("application_status_changes", "deadline_events", "draft_records",
                                  "application_activity")}

    def test_apply_moves_the_tracker_schedules_deadlines_and_saves_unsent_drafts(self):
        pairs, ctx = context(self.db, dry_run=False)
        result = by_step(act(pairs, ctx))
        self.assertEqual(result[("tracker", "m1")][0], DONE)
        self.assertIn("now applied", result[("tracker", "m1")][1])
        self.assertIn("now interview", result[("tracker", "m2")][1])
        self.assertEqual(result[("tracker", "m3")][0], SKIPPED)              # a reminder isn't a status change
        self.assertEqual(result[("deadlines", "m3")], (DONE, "deadline created: 2026-11-05T17:00:00.000000Z"))
        self.assertEqual(result[("deadlines", "m4")], (DONE, "deadline updated: 2026-11-12T17:00:00.000000Z"))
        self.assertEqual(result[("deadlines", "m5")][0], REVIEW)
        self.assertIn("doesn't name one tracked opportunity", result[("deadlines", "m5")][1])
        self.assertEqual(result[("deadlines", "m6")][0], REVIEW)
        self.assertIn("past", result[("deadlines", "m6")][1])
        self.assertEqual((result[("drafts", "m1")][0], result[("drafts", "m2")][0]), (DONE, DONE))
        self.assertFalse([key for key in result if key[1] == "m7"])           # scams get no action at all

        with closing(sqlite3.connect(self.db)) as conn:
            [app] = list_applications(conn)
        self.assertEqual(app.status.value, "interview")
        self.assertEqual(len(ctx.calendar.events_by_key), 1)                   # the extension moved the one event
        [(event_id, _, deadline_at, _)] = ctx.calendar.events_by_key.values()
        self.assertEqual(deadline_at, LATER)
        self.assertEqual(len(ctx.gmail.drafts_by_key), 2)
        with closing(sqlite3.connect(self.db)) as conn:
            kinds = {line["kind"] for line in activity_for_email(conn, "gmail:m2")}
        self.assertIn("gmail_draft", kinds)

    def test_second_run_changes_nothing(self):
        pairs, ctx = context(self.db, dry_run=False)
        act(pairs, ctx)
        before = self.counts()
        again = by_step(act(pairs, ctx))
        self.assertEqual(self.counts(), before)
        for key in (("tracker", "m1"), ("tracker", "m2"), ("deadlines", "m4"), ("drafts", "m1"), ("drafts", "m2")):
            self.assertEqual(again[key][0], ALREADY_DONE, key)
        self.assertEqual((len(ctx.calendar.events_by_key), len(ctx.gmail.drafts_by_key)), (1, 2))

    def test_dry_run_plans_without_writing_or_calling_providers(self):
        before = self.counts()
        pairs, ctx = context(self.db, dry_run=True)
        result = by_step(act(pairs, ctx))
        self.assertEqual(self.counts(), before)
        self.assertEqual((ctx.calendar.events_by_key, ctx.gmail.drafts_by_key), ({}, {}))
        self.assertEqual(result[("tracker", "m1")][0], PLANNED)
        self.assertEqual(result[("deadlines", "m3")], (PLANNED, "would schedule 2026-11-05T17:00:00.000000Z"))
        self.assertEqual(result[("drafts", "m2")][0], PLANNED)
        self.assertEqual(result[("deadlines", "m5")][0], REVIEW)

    def test_command_refuses_offline_apply_and_another_account_on_a_gmail_database(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["run", "--db", str(self.db), "--fake", "--apply"]), 2)
            self.assertEqual(main(["run", "--db", str(self.db), "--gmail", "--account", "other@example.test",
                                   "--token", str(Path(self.tmp.name) / "unused.json")]), 2)
        self.assertIn("another Gmail account", err.getvalue())


class FixtureCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "keeper.db"
        gold = {item["id"]: item["gold"] for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["emails"]}
        store = ResultStore(self.db)
        for email in load_emails(FIXTURE):
            g = gold[email.id]
            store.save(email, decision(email, TriageBucket(g["bucket"]), career=g["career"],
                                       deadline=from_storage(g["deadline"]), scam=tuple(g["scam_signals"])))

    def tearDown(self):
        self.tmp.cleanup()

    def test_saved_decisions_round_trip_oldest_first(self):
        loaded = saved_decisions(self.db)
        self.assertEqual(len(loaded), 75)
        self.assertEqual(sorted(loaded, key=lambda p: (p[0].received_at, p[0].id)), loaded)
        self.assertEqual(saved_decisions(Path(self.tmp.name) / "absent.db"), [])

    def test_offline_run_over_the_demo_fixture(self):
        for argv in (["run", "--db", str(self.db), "--fake"], ["run", "--db", str(self.db), "--fake", "--apply"]):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(argv), 0)
                report = json.loads(out.getvalue())
                self.assertEqual(report["decisions"], 75)
                self.assertNotIn(FAILED, json.dumps(report["summary"]))
                # Fixture mail isn't Gmail, and nothing in it names a tracked opportunity: every one of
                # the 19 deadlines is found (#14 added the missing cues) and held for review.
                self.assertEqual(set(report["summary"]["drafts"]), {SKIPPED})
                self.assertEqual(report["summary"]["deadlines"], {REVIEW: 19})

    def test_a_source_is_required_and_gmail_needs_its_account(self):
        for argv in (["run", "--db", str(self.db)], ["run", "--db", str(self.db), "--gmail"],
                     ["run", "--db", str(self.db), "--fake", "--account", ACCOUNT]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(argv)


class InterfaceTest(unittest.TestCase):
    def test_every_step_has_the_shared_shape(self):
        self.assertEqual([s.NAME for s in STEPS], ["tracker", "deadlines", "drafts"])
        for step in (tracker, deadlines, drafts):
            self.assertTrue(callable(step.wants) and callable(step.run))

    def test_no_step_takes_junk_newsletters_review_or_scams(self):
        email = gmail_email(9, "Hi", "Body", 1)
        for bucket in (TriageBucket.JUNK, TriageBucket.NEWSLETTER, None):
            self.assertFalse(any(s.wants(email, decision(email, bucket, deadline=AT)) for s in STEPS), bucket)
        scam = decision(email, TriageBucket.DUE, deadline=AT, scam=("urgency",))
        self.assertFalse(any(s.wants(email, scam) for s in STEPS))
        self.assertFalse(tracker.wants(email, replace(decision(email, TriageBucket.ACTIONABLE), career=None)))

    def test_outcome_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            Outcome("drafts", "gmail:m1", "sent", "never a status")

    def test_provider_error_fails_one_item_and_bad_credentials_stop_the_run(self):
        def step(error):
            return SimpleNamespace(NAME="tracker", wants=lambda e, r: True,
                                   run=lambda e, r, ctx: (_ for _ in ()).throw(error))
        pairs = [(e, decision(e, b)) for e, b, _ in SCENARIO[:3]]
        ctx = ActionContext(Path("unused.db"), FakeGmail(()), FakeCalendar(), None, lambda: NOW)
        outcomes = act(pairs, ctx, steps=(step(ProviderUnavailable("private detail")),))
        self.assertEqual([(o.status, o.detail) for o in outcomes], [(FAILED, "ProviderUnavailable")] * 3)
        self.assertEqual(summarize(outcomes, steps=(step(None),)), {"tracker": {FAILED: 3}})
        with self.assertRaises(AuthenticationError):
            act(pairs, ctx, steps=(step(AuthenticationError("expired")),))


if __name__ == "__main__":
    unittest.main()
