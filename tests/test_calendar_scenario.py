import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CalendarScenarioTest(unittest.TestCase):
    def test_script_is_deterministic_and_shows_one_extended_deadline(self):
        command = [sys.executable, "-m", "demo.calendar_lifecycle"]
        first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report["mode"], "offline-synthetic")
        self.assertEqual([item["result"] for item in report["updates"]], ["created", "updated"])
        self.assertEqual([item["replay"] for item in report["updates"]], ["unchanged", "unchanged"])
        event = report["event"]
        self.assertEqual(event["deadline_at"], "2026-12-15T17:00:00.000000Z")
        self.assertEqual(event["provider_event_id"], "fake-event:1")
        self.assertEqual({item["provider_event_id"] for item in report["updates"]},
                         {event["provider_event_id"]})
        self.assertIn("extended to 2026-12-15T17:00:00Z", event["source_evidence"])
        self.assertEqual(event["source_email_id"], "scenario:email:extension")
        self.assertEqual([item["kind"] for item in report["application_activity"]],
                         ["calendar_created", "calendar_updated"])
        self.assertEqual(report["application_activity"][-1]["source_evidence"],
                         event["source_evidence"])
        self.assertTrue(all(item["provider_id"] == event["provider_event_id"]
                            for item in report["application_activity"]))


if __name__ == "__main__":
    unittest.main()
