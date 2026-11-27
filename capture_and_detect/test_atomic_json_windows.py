"""atomic_json survives another program briefly holding the file open on Windows."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cup_inventory


class AtomicJsonRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "robot_recovery.json"
        self.real_replace = os.replace

    def flaky(self, failures):
        calls = []
        def replace(source, target):
            calls.append(target)
            if len(calls) <= failures:
                raise PermissionError(5, "Access is denied")
            self.real_replace(source, target)
        return replace, calls

    def test_windows_lock_is_retried_and_the_document_is_written(self):
        replace, calls = self.flaky(3)
        with patch.object(cup_inventory, "_RETRY_REPLACE", True), patch("cup_inventory.os.replace", side_effect=replace):
            cup_inventory.atomic_json(self.path, {"schema_version": 1, "recovery_required": True})
        self.assertEqual(len(calls), 4)
        self.assertEqual(json.loads(self.path.read_text()), {"schema_version": 1, "recovery_required": True})
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])   # no temp file left behind

    def test_a_lock_that_does_not_go_away_still_fails_and_cleans_up(self):
        replace, calls = self.flaky(10 ** 6)
        with patch.object(cup_inventory, "_RETRY_REPLACE", True), \
                patch.object(cup_inventory, "REPLACE_RETRY_SECONDS", 0.2), \
                patch("cup_inventory.os.replace", side_effect=replace):
            started = time.monotonic()
            with self.assertRaises(PermissionError):
                cup_inventory.atomic_json(self.path, {"a": 1})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertGreater(len(calls), 1)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_other_platforms_and_other_errors_are_not_retried(self):
        replace, calls = self.flaky(1)
        with patch.object(cup_inventory, "_RETRY_REPLACE", False), patch("cup_inventory.os.replace", side_effect=replace):
            with self.assertRaises(PermissionError):
                cup_inventory.atomic_json(self.path, {"a": 1})
        self.assertEqual(len(calls), 1)
        with patch.object(cup_inventory, "_RETRY_REPLACE", True), \
                patch("cup_inventory.os.replace", side_effect=OSError("disk full")) as failing:
            with self.assertRaises(OSError):
                cup_inventory.atomic_json(self.path, {"a": 1})
        self.assertEqual(failing.call_count, 1)


if __name__ == "__main__":
    unittest.main()
