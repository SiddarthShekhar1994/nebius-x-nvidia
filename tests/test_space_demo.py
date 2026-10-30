"""The hosted demo (space/) plays the story and visitor emails through Keeper's code, offline here.

The triage fake answers with each story email's gold label, so these tests check the demo's
plumbing and the page API, not the live model.
"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from keeper_core.adapters import ModelAnswer
from keeper_core.contracts import ModelMetadata
from keeper_core.fakes import FakeModel
from space import engine, server

GOLD = {item["id"]: item["gold"] for part in (1, 2) for item in engine.story(part)}
OFFER = ("Jordan Lee <jordan.lee@careers.example.test>", "Your Software Engineer Intern offer",
         "Example Inc is pleased to offer you the Software Engineer Intern position for summer 2027.")


class GoldTriage:
    """Nano stand-in: story emails get their gold label, visitor emails get `visitor`."""

    def __init__(self, visitor=None):
        self.visitor = visitor or {"bucket": "actionable", "reason": "An internship offer.", "career": True,
                                   "scam_signals": [], "deadline": None, "deadline_evidence": None,
                                   "confidence": 0.9}
        self.calls = 0

    def complete(self, task_type, prompt):
        self.calls += 1
        email, _ = json.JSONDecoder().raw_decode(prompt.split("EMAIL_JSON:\n", 1)[1])
        gold = GOLD.get(email["id"].removeprefix("gmail:"))
        answer = self.visitor if gold is None else {
            "bucket": gold["bucket"], "reason": "Gold label.", "career": gold["career"],
            "scam_signals": gold["scam_signals"], "deadline": gold["deadline"],
            "deadline_evidence": gold["deadline_evidence"], "confidence": 0.9,
        }
        return ModelAnswer(json.dumps(answer), ModelMetadata("nano", "nano", "t"))


def drafts():
    return FakeModel({"draft": ModelAnswer("Thank you, I received it.", ModelMetadata("super", "super", "t"))})


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = engine.Session(Path(self.tmp.name) / "visit")
        self.triage, self.drafts = GoldTriage(), drafts()

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, action, email=None):
        engine.check(self.session, action)
        events = []
        engine.run_step(self.session, action, lambda session: (self.triage, self.drafts), events.append, email=email)
        return {event["card"]["email"]["id"]: event["card"] for event in events if event["kind"] == "card"}

    def outcomes(self, card):
        return {outcome["step"]: (outcome["status"], outcome["detail"]) for outcome in card["outcomes"]}

    def test_the_story_moves_the_tracker_and_one_calendar_event(self):
        self.step("track")
        first = self.step("part1")
        self.assertIn("now applied", self.outcomes(first["story-01"])["tracker"][1])
        self.assertIn("now interview", self.outcomes(first["story-02"])["tracker"][1])
        self.assertEqual(self.outcomes(first["story-03"])["deadlines"],
                         ("done", "deadline created: 2026-11-06T17:00:00.000000Z"))
        self.assertEqual(first["story-03"]["labels"], ["KEEPER_DUE"])
        self.assertEqual(len(first["story-02"]["drafts"]), 1)
        second = self.step("part2")
        self.assertEqual(self.outcomes(second["story-04"])["deadlines"],
                         ("done", "deadline updated: 2026-11-13T17:00:00.000000Z"))
        self.assertEqual(second["story-05"]["outcomes"], [])  # the scam gets no tracker change, event or draft
        self.assertEqual(second["story-05"]["labels"], ["KEEPER_ACTIONABLE", "KEEPER_SCAM_REVIEW"])
        state = engine.state(self.session)
        [app] = state["tracker"]
        self.assertEqual([change["status"] for change in app["history"]], ["found", "applied", "interview"])
        self.assertEqual([event["at"] for event in state["calendar"]], ["2026-11-13T17:00:00.000000Z"])
        self.assertEqual([draft["email_id"] for draft in state["drafts"]], ["story-01", "story-02"])
        self.assertEqual(state["done"], ["part1", "part2", "track"])
        self.assertEqual(len(state["cards"]), 5)
        self.assertEqual(self.triage.calls, 5)

    def test_steps_run_once_and_in_order(self):
        with self.assertRaises(engine.StepError):
            engine.check(self.session, "part1")
        self.step("track")
        with self.assertRaises(engine.StepError):
            engine.check(self.session, "track")
        with self.assertRaises(engine.StepError):
            engine.check(self.session, "part2")

    def test_a_named_recruiters_offer_gets_a_super_draft(self):
        self.step("track")
        self.step("part1")
        card = self.step("email", engine.written_email(self.session, *OFFER))["visitor-01"]
        self.assertIn("now decision", self.outcomes(card)["tracker"][1])
        [draft] = card["drafts"]
        self.assertEqual((draft["flags"], draft["body"]), ([], "Thank you, I received it."))
        [app] = engine.state(self.session)["tracker"]
        self.assertEqual((app["status"], app["outcome"]), ("decision", "offered"))

    def test_a_visitor_scam_gets_warnings_and_no_draft(self):
        self.triage.visitor = {**self.triage.visitor, "reason": "Fee for a job."}
        email = engine.written_email(self.session, "Campus Jobs Desk <campus.jobs.desk@gmail.com>",
                                     "Remote internship, $200/hr, no interview",
                                     "Pay the $50 registration fee today to hold your spot.")
        card = self.step("email", email)["visitor-01"]
        self.assertEqual(set(card["triage"]["scam_signals"]),
                         {"upfront_fee", "urgency", "unrealistic_pay", "free_mail_sender"})
        self.assertEqual((card["outcomes"], card["drafts"]), ([], []))

    def test_visitor_emails_are_bounded(self):
        for sender, subject, body in (("", "Hi", "Body"), ("No address", "Hi", "Body"),
                                      (OFFER[0], "Hi", "x" * 1501), (OFFER[0], None, "Body")):
            with self.subTest(sender=sender, subject=subject), self.assertRaises(engine.StepError):
                engine.written_email(self.session, sender, subject, body)

    def test_budget_resets_each_day(self):
        days = iter(["2026-10-30", "2026-10-30", "2026-10-31"])
        budget = engine.Budget(5, clock=lambda: engine.CLOCK.replace(day=int(next(days)[-2:])))
        self.assertTrue(budget.reserve(5))
        self.assertFalse(budget.reserve(1))
        self.assertTrue(budget.reserve(5))


class ServerTest(unittest.TestCase):
    def start(self, *, live=True, per_day=100):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        triage, draft_model = GoldTriage(), drafts()
        demo = server.Demo(Path(self.tmp.name), lambda session: (triage, draft_model), engine.Budget(per_day),
                           live=live, model_names={"triage": "nano", "drafts": "super"})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_for(demo, log=False))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.session = "ab" * 16

    def call(self, path, body=None):
        request = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read()), response.headers
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read()), exc.headers

    def run_step(self, action, **extra):
        status, data, _ = self.call("/api/run", {"session": self.session, "action": action, **extra})
        self.assertEqual(status, 202, data)
        for _ in range(200):
            status, job, _ = self.call(f"/api/job?id={data['job']}&since=0")
            if job["done"]:
                self.assertIsNone(job["error"])
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_page_and_story_over_http(self):
        self.start()
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            page = response.read().decode()
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("Keeper live demo", page)
        self.run_step("track")
        part1 = self.run_step("part1")
        self.assertEqual([e["card"]["email"]["id"] for e in part1["events"] if e["kind"] == "card"],
                         ["story-01", "story-02", "story-03"])
        self.assertEqual(part1["state"]["tracker"][0]["status"], "interview")
        status, state, _ = self.call(f"/api/state?session={self.session}")
        self.assertEqual((status, state["done"]), (200, ["part1", "track"]))

    def test_requests_are_checked(self):
        self.start()
        self.assertEqual(self.call("/api/state?session=nope")[0], 400)
        self.assertEqual(self.call("/api/run", {"session": self.session, "action": "part2"})[0], 409)
        self.assertEqual(self.call("/api/run", {"session": self.session, "action": "explode"})[0], 400)
        self.assertEqual(self.call("/api/job?id=missing&since=0")[0], 404)

    def test_model_steps_need_the_key_and_the_budget(self):
        self.start(live=False)
        self.run_step("track")  # no model calls, so it works without a key
        self.assertEqual(self.call("/api/run", {"session": self.session, "action": "part1"})[0], 503)
        self.start(per_day=4)
        self.run_step("track")
        status, data, _ = self.call("/api/run", {"session": self.session, "action": "part1"})
        self.assertEqual(status, 429, data)


if __name__ == "__main__":
    unittest.main()
