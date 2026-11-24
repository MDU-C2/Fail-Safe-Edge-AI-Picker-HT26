"""The installed verifier runs without pytest or image/ML dependencies."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class QueueVerifierTests(unittest.TestCase):
    def test_standalone_offline_verifier_covers_finite_runtime_and_queue_failures(self):
        verifier = Path(__file__).with_name("verify_queue_runtime.py")
        repo = verifier.parents[2]
        result = subprocess.run([sys.executable, str(verifier), "--app-root", str(repo)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["real_network_calls"], 0)
        self.assertEqual(report["model_workers_started"], 0)
        self.assertFalse(report["persistent_state_used"])
        self.assertEqual(report["finite_batch"]["placement_ids"], [1, 1, 1])
        self.assertEqual(report["finite_batch"]["queue_submissions"], 5)
        self.assertEqual(report["initial_grid_only"]["home_cycles"], 1)
        self.assertEqual(report["initial_grid_only"]["captures"], 1)
        self.assertFalse(report["failure_checks"]["ambiguous_submit_retried"])


if __name__ == "__main__":
    unittest.main()
