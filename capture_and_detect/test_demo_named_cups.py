"""Synthetic saved-cup flow exercises normal confirmation without image libraries."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cup_commands import CupSession
from cup_inventory import SelectionError
from demo_named_cups import DemoNamedCupStore, DemoNamedLanguageController, annotate_demo_inventory
from named_cups import NamedCupStore
from test_cup_selection import RobotDouble, detection, inventory
from test_support_grid import grid_fixture


class DemoNamedCupTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        network = patch("requests.Session.request", side_effect=AssertionError("Network forbidden"))
        self.network = network.start()
        self.addCleanup(network.stop)
        modules = patch.dict(sys.modules, {"cv2": None, "numpy": None})
        modules.start()
        self.addCleanup(modules.stop)
        self.samples = [("teal", "teal", 100, 500), ("white", "white", 200, 550)]
        self.sequence = 0
        self.captures = []
        self.robot = RobotDouble()
        self.robot.dry_run = True
        self.session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: 100,
                                  placing_grid=grid_fixture(self.directory / "grid"))
        self.interpreter = Mock()
        self.language = DemoNamedLanguageController(self.session, self.interpreter, directory=self.directory,
                                                    audit_dir=self.directory / "audit", clock=lambda: 100)

    def capture(self, purpose):
        self.sequence += 1
        self.captures.append(purpose)
        objects, identities = [], {}
        for stable_id, identity, u, x in self.samples:
            detection_id = stable_id + "-CUP"
            objects.extend((detection("CUP", u, x=x, detection_id=detection_id),
                            detection("CUP BASE", u, x=x, detection_id=stable_id + "-BASE")))
            identities[detection_id] = identity
        result = inventory(capture=f"demo_{self.sequence}_{purpose}", sequence=self.sequence, objects=objects)
        result.update(gateway="demo", model={"demo": True}, images={"annotated": "illustration.svg"})
        annotate_demo_inventory(result, identities)
        return result, 100

    def save_white(self):
        self.session.scan()
        result = self.language.save_cup(capture_id=self.session.inventory["capture"], cup_id=2, name="Kasun\u2019s cup")
        self.assertIn("synthetic", result["message"])
        return result["cup"]

    def test_save_persists_sample_thumbnail_without_image_library_or_movement(self):
        saved = self.save_white()
        reopened = DemoNamedCupStore(self.directory)
        reference = reopened.get("KASUN'S CUP")
        self.assertEqual(reference["id"], saved["id"])
        self.assertEqual(reference["demo_identity"], "white")
        self.assertTrue(reference["image_bytes"].startswith(b"\xff\xd8"))
        self.assertEqual(reopened.list(), [saved])
        self.assertTrue(json.loads(reopened.path.read_text())["demo_only"])
        for key in ("robot_mm", "x", "y", "z", "image_bytes", "demo_identity"):
            self.assertNotIn(key, saved)
        self.assertEqual(self.robot.jobs, [])
        self.network.assert_not_called()

    def test_renumbered_fresh_cups_use_identity_then_normal_preview_and_confirmation(self):
        saved = self.save_white()
        self.samples.insert(0, ("orange", "orange", 40, 450))
        self.session.scan()
        proposal = self.language.propose_saved(saved["id"])
        self.assertEqual(proposal["kind"], "proposal")
        self.assertEqual(self.language.pending["record"]["cup_id"], 3)
        self.assertEqual(self.captures, ["scan", "scan"])
        self.assertEqual(self.robot.jobs, [])
        self.assertIn("Scripted demo", proposal["message"])
        self.language.confirm(proposal["token"])
        self.assertEqual(self.captures, ["scan", "scan"])
        self.assertTrue(self.robot.jobs)  # Only the in-memory dry-run double runs.
        self.interpreter.interpret.assert_not_called()
        self.network.assert_not_called()
        with self.assertRaises(SelectionError):
            self.language.confirm(proposal["token"])

    def test_absent_and_ambiguous_synthetic_identity_do_not_fall_back_to_saved_id(self):
        saved = self.save_white()
        cases = [[("teal", "teal", 200, 550)],
                 [("white", "white", 100, 500), ("copy", "white", 200, 550)]]
        for samples in cases:
            with self.subTest(samples=samples):
                self.samples = samples
                self.session.scan()
                with self.assertRaisesRegex(SelectionError, "uniquely identify"):
                    self.language.propose_saved(saved["id"])
                self.assertIsNone(self.language.pending)
                self.assertEqual(self.robot.jobs, [])

    def test_changed_camera_scene_does_not_replace_the_reviewed_snapshot(self):
        saved = self.save_white()
        proposal = self.language.propose_saved(saved["id"])
        self.samples[1] = ("orange", "orange", 200, 550)
        self.language.confirm(proposal["token"])
        self.assertTrue(self.robot.jobs)
        self.assertEqual(self.captures, ["scan"])
        self.assertIsNone(self.language.pending)

    def test_demo_cannot_attach_to_live_robot_or_save_a_real_camera_inventory(self):
        self.robot.dry_run = False
        with self.assertRaisesRegex(SelectionError, "offline dry-run"):
            DemoNamedLanguageController(self.session, self.interpreter, directory=self.directory)
        real = inventory()
        with self.assertRaisesRegex(SelectionError, "offline demo"):
            annotate_demo_inventory(real, {0: "white"})
        with self.assertRaisesRegex(SelectionError, "offline demo"):
            self.language.named_cups.save("Real cup", real, 1)
        self.assertEqual(self.robot.jobs, [])

    def test_real_store_cannot_read_demo_records_and_reference_tampering_is_rejected(self):
        self.save_white()
        store = self.language.named_cups
        with self.assertRaises(SelectionError):
            NamedCupStore(store.path, self.directory).list()
        value = json.loads(store.path.read_text())
        value["cups"][0]["demo_identity"] = "teal"
        store.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(SelectionError, "Invalid sample"):
            store.list()
        self.assertEqual(self.robot.jobs, [])


if __name__ == "__main__":
    unittest.main()
