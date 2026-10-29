import json
import subprocess
import sys
import unittest
from pathlib import Path


class ReviewedRadarScenarioTest(unittest.TestCase):
    def test_review_approval_extension_and_qualified_delivery_are_deterministic(self):
        command = [sys.executable, "-m", "demo.reviewed_radar_lifecycle"]
        root = Path(__file__).resolve().parents[1]
        first = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
        second = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
        self.assertEqual(first.stdout, second.stdout)
        result = json.loads(first.stdout)
        self.assertEqual(result["discovery"], {"eligibility": "unverified", "digest": "empty"})
        self.assertEqual(result["review"]["eligibility"], "eligible")
        self.assertEqual(result["calendar"], {
            "first_approval": "created", "fresh_approval": "updated", "retry": "unchanged",
            "stale_review": "rejected", "provider_event_id": "fake-event:1",
        })
        self.assertEqual(result["digest"], {"after_review": "sent", "retry": "sent", "next_day": "empty"})
        self.assertEqual(result["counts"], {"opportunities": 1, "observations": 2, "reviews": 2,
                                            "approvals": 2, "calendar_events": 1, "telegram_messages": 1})
