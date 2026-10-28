import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ApplicationScenarioTest(unittest.TestCase):
    def test_script_is_deterministic_and_shows_one_complete_history(self):
        command = [sys.executable, "-m", "demo.application_lifecycle"]
        first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual([entry["status"] for entry in report["history"]],
                         ["found", "applied", "interview", "decision"])
        self.assertEqual([update["result"] for update in report["updates"]],
                         ["updated", "updated", "updated"])
        self.assertEqual(report["decision_outcome"], "rejected")
        self.assertEqual(len({update["email_id"] for update in report["updates"]}), 3)
        self.assertTrue(all(entry["source_evidence"] for entry in report["history"]))


if __name__ == "__main__":
    unittest.main()
