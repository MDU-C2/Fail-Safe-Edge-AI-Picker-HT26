"""Optional grip CSV records follow completed/interrupted real executions only."""

from contextlib import redirect_stdout
import copy
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from capture_and_detect import CaptureStore
from cup_commands import CupSession
from test_cup_selection import RobotDouble, inventory


class GripLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.store = CaptureStore(self.folder)

    def rows(self):
        with (self.folder / "grips.csv").open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    def record(self, status):
        result = inventory()
        result["cups"][0].update(status=status, grip_left_mm=21.5, grip_right_mm=19.25,
            execution={"action": "goto"},
            last_verification={"capture": "verification-1", "grip_geometry": {"diameter": 82}})
        return result

    def test_only_final_execution_statuses_emit_one_row_per_verified_cup(self):
        for status in ("available", "needs_rescan", "command_pending"):
            self.store.save(self.record(status))
            self.assertFalse((self.folder / "grips.csv").exists())
        for index, status in enumerate(("placed", "replaced", "movement_unconfirmed"), 1):
            record = self.record(status)
            record["cups"][0]["last_verification"]["capture"] = f"verification-{index}"
            self.store.save(record)
            self.store.save(copy.deepcopy(record))
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows], ["placed", "replaced", "movement_unconfirmed"])
        self.assertTrue(all(row["grip_left_mm"] == "21.5" and row["grip_right_mm"] == "19.25" for row in rows))
        self.assertTrue(all(row["diameter_mm"] == "82" for row in rows))

    def test_hover_status_does_not_create_a_grip_record(self):
        record = self.record("replaced")
        record["cups"][0]["execution"]["action"] = "hover"
        self.store.save(record)
        self.assertFalse((self.folder / "grips.csv").exists())

    def test_late_stop_records_status_correction_for_the_same_verification(self):
        record = self.record("placed")
        self.store.save(record)
        record["cups"][0]["status"] = "movement_unconfirmed"
        self.store.save(record)
        self.store.save(copy.deepcopy(record))
        self.assertEqual([row["status"] for row in self.rows()],
                         ["placed", "movement_unconfirmed"])

    def test_actual_dry_run_saves_observation_without_grip_csv(self):
        robot = RobotDouble()
        robot.dry_run = True
        capture = Mock(side_effect=[(inventory(), 99),
                                    (inventory(capture="verification-1", sequence=11), 99)])
        session = CupSession(capture, self.store.save, robot, "cal-1", left_only=True, clock=lambda: 100)
        with redirect_stdout(io.StringIO()):
            session.scan()
            session.execute_selection(action="goto", capture_id="scan-1", cup_id=1)
        self.assertTrue((self.folder / "cups.json").is_file())
        self.assertFalse((self.folder / "grips.csv").exists())


if __name__ == "__main__":
    unittest.main()
