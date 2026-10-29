import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RadarScenarioTest(unittest.TestCase):
    def test_script_is_deterministic_and_proves_retry_and_cross_run_dedupe(self):
        command = [sys.executable, "-m", "demo.radar_lifecycle"]
        first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(first.stdout, second.stdout)

        report = json.loads(first.stdout)
        self.assertEqual(report["mode"], "offline-synthetic")
        self.assertEqual(report["candidate"]["title"], "Software Engineer Intern")
        self.assertEqual(report["candidate"]["organization"], "Example Inc")
        self.assertEqual(report["candidate"]["snippet"],
                         "Join the AI infrastructure team building developer tools for production.")
        self.assertEqual(report["candidate"]["scoring"]["mode"], "simulated")
        self.assertEqual(report["candidate"]["scoring"]["score"], 8)
        self.assertEqual(report["candidate"]["eligibility"], "unverified")
        self.assertIsNone(report["candidate"]["deadline"])
        self.assertEqual([run["dedupe"] for run in report["runs"]],
                         ["new_candidate", "same_run_retry", "known_candidate_not_resent"])
        self.assertEqual([run["digest"] for run in report["runs"]], ["sent", "sent", "empty"])
        self.assertEqual(report["counts"], {
            "stored_opportunities": 1,
            "history_observations": 2,
            "telegram_messages": 1,
            "calendar_writes": 0,
        })


if __name__ == "__main__":
    unittest.main()
