"""The dashboard publishes camera evidence without changing slot ownership."""

from contextlib import redirect_stdout
import copy
import io
import tempfile
import unittest
from unittest.mock import Mock

from test_web_controller import WebFixture
from web_controller import WebController


class WebPlacementObservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = WebFixture(directory.name)
        self.controller = WebController(self.fixture.factory)
        self.addCleanup(self.controller.close)
        self.controller.session = self.fixture.session
        self.controller.language = self.fixture.language
        with redirect_stdout(io.StringIO()):
            self.fixture.session.scan()

    def test_camera_evidence_and_released_ids_are_copied_into_public_scan(self):
        observation = {"frame_count": 3, "applied": True, "message": "Placement area checked.",
                       "released_ids": [2], "slots": [
                           {"id": 2, "state": "clear", "reason": "Table visible in all frames",
                            "polygon_px": [[1, 2], [3, 2], [3, 4], [1, 4]], "valid_fraction": 0.98}]}
        self.fixture.session.inventory["placement_observation"] = observation
        expected = copy.deepcopy(observation)
        self.controller.publish_session()
        self.assertEqual(self.controller.state()["inventory"]["placement_observation"], expected)
        observation["released_ids"].clear()
        observation["slots"][0]["state"] = "unknown"
        state = self.controller.state()
        self.assertEqual(state["inventory"]["placement_observation"], expected)
        state["inventory"]["placement_observation"]["slots"].clear()
        self.assertEqual(self.controller.state()["inventory"]["placement_observation"], expected)
        self.assertEqual(self.fixture.robot.commands, [])

    def test_vision_blocked_and_reservation_flags_are_not_lost_in_public_grid(self):
        fingerprint = "a" * 64
        self.fixture.session.placing_grid.snapshot = Mock(return_value={"fingerprint": fingerprint, "locations": [
            {"id": 1, "x": 400, "y": -200, "z": 230, "occupied": False, "vision_blocked": True},
            {"id": 2, "x": 480, "y": -200, "z": 230, "occupied": True,
             "reserved": True, "reservation_id": "private-owner", "vision_blocked": True},
            {"id": 3, "x": 560, "y": -200, "z": 230, "occupied": False, "vision_blocked": False},
        ]})
        self.controller.publish_session()
        self.assertEqual(self.controller.state()["placement_grid"]["fingerprint"], fingerprint)
        locations = self.controller.state()["placement_grid"]["locations"]
        self.assertTrue(locations[0]["vision_blocked"])
        self.assertFalse(locations[0]["occupied"])
        self.assertTrue(locations[1]["reserved"])
        self.assertTrue(locations[1]["vision_blocked"])
        self.assertNotIn("reservation_id", locations[1])
        self.assertFalse(locations[2]["vision_blocked"])
        self.assertEqual(self.fixture.robot.commands, [])

    def test_cup_summary_and_distinct_obstacle_evidence_are_copied_without_changing_grid(self):
        observation = {"frame_count": 3, "applied": True, "message": "Placement area checked.",
                       "released_ids": [], "cup_summary": {
                           "assigned_count": 2, "location_ids": [2, 3],
                           "min_assigned_count": 1, "max_assigned_count": 2,
                           "stable_cell_counts": False}, "slots": [
                           {"id": 2, "state": "occupied", "cup_count": 1, "evidence": ["cup"]},
                           {"id": 3, "state": "occupied", "cup_count": 1, "evidence": ["cup", "raised_depth"]},
                           {"id": 9, "state": "occupied", "cup_count": 0, "evidence": ["raised_depth"]}]}
        grid_before = self.fixture.session.placing_grid.snapshot()
        self.fixture.session.inventory["placement_observation"] = observation
        expected = copy.deepcopy(observation)
        self.controller.publish_session()
        public = self.controller.state()["inventory"]["placement_observation"]
        self.assertEqual(public, expected)
        observation["cup_summary"]["location_ids"].clear()
        observation["slots"][2]["evidence"].append("cup")
        public["cup_summary"]["assigned_count"] = 10
        public["cup_summary"]["location_ids"].append(9)
        public["slots"][2]["evidence"].clear()
        self.assertEqual(self.controller.state()["inventory"]["placement_observation"], expected)
        self.assertEqual(self.fixture.session.placing_grid.snapshot(), grid_before)
        self.assertEqual(self.fixture.robot.commands, [])

    def test_private_grid_snapshot_and_frame_sequences_are_not_exposed(self):
        self.fixture.session.inventory["placement_observation"] = {
            "frame_count": 3, "applied": False, "message": "Location reserved.",
            "released_ids": [], "slots": [{"id": 1, "state": "unknown", "reserved": True}],
            "frame_sequences": [100, 101, 102],
            "grid_snapshot": {"fingerprint": "private-fingerprint", "locations": [
                {"id": 1, "reserved": True, "reservation_id": "private-owner"}]},
        }
        self.controller.publish_session()
        observation = self.controller.state()["inventory"]["placement_observation"]
        self.assertEqual(set(observation), {"frame_count", "applied", "message", "released_ids", "slots"})
        self.assertTrue(observation["slots"][0]["reserved"])

    def test_legacy_or_demo_scan_does_not_invent_camera_evidence(self):
        self.controller.publish_session()
        self.assertNotIn("placement_observation", self.controller.state()["inventory"])


if __name__ == "__main__":
    unittest.main()
