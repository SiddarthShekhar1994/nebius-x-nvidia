"""The shared interface for keeper act: saved decisions in, one outcome per step and email out."""
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from actions import deadlines, drafts, tracker
from actions.__main__ import STEPS, act, main, summarize
from actions.base import FAILED, NOT_BUILT, ActionContext, Outcome
from keeper_core.adapters import ProviderUnavailable
from keeper_core.contracts import TriageBucket, TriageResult
from keeper_core.fakes import FakeCalendar, FakeGmail
from keeper_core.time import from_storage
from keeper_core.workflow import stable_key
from triage.preview import load_emails
from triage.store import ResultStore, saved_decisions

FIXTURE = Path(__file__).resolve().parents[1] / "demo/fixtures/inbox_75.json"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def gold_decisions():
    """Fixture emails paired with triage results equal to their gold labels."""
    gold = {item["id"]: item["gold"] for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["emails"]}
    pairs = []
    for email in load_emails(FIXTURE):
        g = gold[email.id]
        pairs.append((email, TriageResult(
            id=stable_key("triage", email.id), email_id=email.id, bucket=TriageBucket(g["bucket"]),
            reason="gold label", career=g["career"], scam_signals=tuple(g["scam_signals"]),
            extracted_deadline=from_storage(g["deadline"]), confidence=None, model=None,
            classified_at=NOW, deadline_evidence=g["deadline"],
        )))
    return pairs


class SavedDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "keeper.db"
        self.pairs = gold_decisions()
        store = ResultStore(self.db)
        for email, result in self.pairs:
            store.save(email, result)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trips_every_saved_decision_oldest_first(self):
        loaded = saved_decisions(self.db)
        self.assertEqual(len(loaded), 75)
        self.assertEqual(sorted(loaded, key=lambda p: (p[0].received_at, p[0].id)), loaded)
        self.assertEqual(dict((e.id, r) for e, r in loaded), dict((e.id, r) for e, r in self.pairs))
        self.assertEqual(dict((e.id, e) for e, _ in loaded), dict((e.id, e) for e, _ in self.pairs))

    def test_missing_database_has_no_decisions(self):
        self.assertEqual(saved_decisions(Path(self.tmp.name) / "absent.db"), [])

    def test_dry_run_command_offers_each_step_its_emails(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["run", "--db", str(self.db), "--fake"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["mode"], report["adapters"], report["decisions"]), ("dry-run", "fake", 75))
        for step in STEPS:
            wanted = sum(step.wants(e, r) for e, r in self.pairs)
            self.assertGreater(wanted, 0, step.NAME)
            self.assertEqual(report["summary"][step.NAME], {NOT_BUILT: wanted})

    def test_live_adapters_are_refused_until_wired(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["run", "--db", str(self.db)])


class SelectionTest(unittest.TestCase):
    def test_steps_ignore_junk_newsletters_review_and_scams(self):
        for email, result in gold_decisions():
            wanted = [s.NAME for s in STEPS if s.wants(email, result)]
            with self.subTest(email=email.id):
                if result.bucket in (TriageBucket.JUNK, TriageBucket.NEWSLETTER) or result.scam_signals:
                    self.assertEqual(wanted, [])
                if result.bucket is TriageBucket.DUE and not result.scam_signals:
                    self.assertIn("deadlines", wanted)
                if result.bucket is TriageBucket.ACTIONABLE and not result.scam_signals:
                    self.assertIn("drafts", wanted)
        for email, result in gold_decisions():
            review = replace(result, bucket=None)
            self.assertFalse(any(s.wants(email, review) for s in STEPS), email.id)

    def test_career_mail_reaches_the_tracker(self):
        tracked = [e.id for e, r in gold_decisions() if tracker.wants(e, r)]
        self.assertTrue(tracked)
        self.assertTrue(all(r.career for e, r in gold_decisions() if e.id in tracked))


class InterfaceTest(unittest.TestCase):
    def test_every_step_has_the_shared_shape(self):
        self.assertEqual([s.NAME for s in STEPS], ["tracker", "deadlines", "drafts"])
        for step in (tracker, deadlines, drafts):
            self.assertTrue(callable(step.wants) and callable(step.run))

    def test_outcome_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            Outcome("drafts", "demo-email-001", "sent", "never a status")

    def test_provider_error_fails_one_item_and_the_run_continues(self):
        broken = SimpleNamespace(
            NAME="tracker", wants=lambda e, r: True,
            run=lambda e, r, ctx: (_ for _ in ()).throw(ProviderUnavailable("private detail")),
        )
        pairs = gold_decisions()[:3]
        ctx = ActionContext(Path("unused.db"), FakeGmail(tuple(e for e, _ in pairs)), FakeCalendar(),
                            None, lambda: NOW)
        outcomes = act(pairs, ctx, steps=(broken,))
        self.assertEqual([o.status for o in outcomes], [FAILED] * 3)
        self.assertEqual({o.detail for o in outcomes}, {"ProviderUnavailable"})
        self.assertEqual(summarize(outcomes, steps=(broken,)), {"tracker": {FAILED: 3}})


if __name__ == "__main__":
    unittest.main()
