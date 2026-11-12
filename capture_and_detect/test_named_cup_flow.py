"""Named-cup integration uses synthetic images, temporary stores and a robot double."""

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from capture_and_detect import handle_input
from cup_commands import CupSession
from cup_inventory import SelectionError
from language_commands import LanguageController
from local_llm import LLMError
from named_cups import NamedCupStore
from recovery_journal import RecoveryJournal
from test_cup_selection import TRANSFER_JOB_COUNT, RobotDouble, detection, inventory
from test_support_grid import grid_fixture


def visual_result(cup_id=1, status="match"):
    return {"status": status, "cup_id": cup_id, "reason": "The blue stripe matches the reference."}


def two_cups():
    return [detection("CUP", 100, detection_id=0), detection("CUP BASE", 100, detection_id=1),
            detection("CUP", 200, x=550, detection_id=2), detection("CUP BASE", 200, x=550, detection_id=3)]


class NamedCupFixture:
    """Real application state, with only image capture and model output replaced."""

    def __init__(self, root, *, dry_run=False):
        self.root = Path(root)
        self.image_root = self.root / "images"
        self.image_root.mkdir(parents=True)
        self.now = 100.0
        self.sequence = 10
        self.captures = []
        self.observations = []
        self.robot = RobotDouble()
        self.robot.dry_run = dry_run
        self.grid = grid_fixture(self.root / "grid")
        self.recovery = RecoveryJournal(self.root / "recovery.json")
        self.store = NamedCupStore(self.root / "saved_cups.json", self.image_root)
        self.matcher = Mock()
        self.matcher.match.return_value = visual_result()
        self.interpreter = Mock()
        self.interpreter.interpret.return_value = {
            "action": "goto", "selector": "id", "cup_id": 1, "message": "Pick cup 1."}
        self.session = CupSession(self.capture, Mock(), self.robot, "cal-1", clock=lambda: self.now,
                                  placing_grid=self.grid, recovery=self.recovery)
        self.language = LanguageController(self.session, self.interpreter, clock=lambda: self.now,
                                           audit_dir=self.root / "audit", named_cups=self.store,
                                           cup_matcher=self.matcher)

    def capture(self, purpose):
        self.sequence += 1
        self.captures.append(purpose)
        options = self.observations.pop(0) if self.observations else {}
        objects = options.get("objects", two_cups())
        result = inventory(capture=f"{purpose}-{self.sequence}", sequence=self.sequence, objects=objects)
        rgb = np.full((180, 400, 3), 235, dtype=np.uint8)
        for cup in result["cups"]:
            if cup["cup_detection_id"] is not None:
                x1, y1, x2, y2 = map(int, cup["bbox_xyxy"])
                cv2.rectangle(rgb, (x1, y1), (x2, y2), (180, 65, 20), -1)
                cv2.line(rgb, (x1 + 5, y1), (x1 + 5, y2), (240, 240, 240), 3)
        path = self.image_root / f"{result['capture']}.jpg"
        if not cv2.imwrite(str(path), rgb):
            raise AssertionError("Could not create synthetic capture")
        result["images"] = {"rgb": str(path), "annotated": str(path)}
        return result, options.get("started", self.now)

    def save_reference(self, name="coffee cup"):
        if self.session.inventory is None:
            self.session.scan()
        return self.language.save_cup(capture_id=self.session.inventory["capture"], cup_id=1, name=name)["cup"]

    def factory(self, controller):
        self.controller = controller
        return self.session, self.language


class NamedCupFlowTests(unittest.TestCase):
    def setUp(self):
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = NamedCupFixture(temporary.name)
        self.language = self.fixture.language
        self.session = self.fixture.session
        self.robot = self.fixture.robot
        self.matcher = self.fixture.matcher

    def proposal(self):
        saved = self.fixture.save_reference()
        proposal = self.language.propose_saved(saved["id"])
        return saved, proposal

    def assert_no_motion(self):
        self.assertEqual(self.robot.jobs, [])
        self.assertFalse(any(slot["occupied"] for slot in self.fixture.grid.snapshot()["locations"]))
        self.assertFalse(self.fixture.recovery.required())

    def test_save_crops_raw_image_and_persists_identity_without_commands(self):
        saved = self.fixture.save_reference()
        self.assertEqual(self.fixture.captures, ["scan"])
        self.assert_no_motion()
        self.matcher.match.assert_not_called()
        self.fixture.interpreter.interpret.assert_not_called()
        reopened = NamedCupStore(self.fixture.store.path, self.fixture.image_root)
        loaded = reopened.get("coffee cup")
        self.assertEqual(loaded["id"], saved["id"])
        self.assertEqual(hashlib.sha256(loaded["image_bytes"]).hexdigest(), saved["reference_sha256"])
        image = cv2.imdecode(np.frombuffer(loaded["image_bytes"], dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertLess(image.shape[0], 180)
        self.assertLess(image.shape[1], 400)
        for key in ("x", "y", "z", "robot_mm", "hover_target", "image_bytes"):
            self.assertNotIn(key, reopened.list()[0])

    def test_saved_proposal_scans_fresh_and_requires_existing_confirmation(self):
        saved, proposal = self.proposal()
        self.assertEqual(proposal["kind"], "proposal")
        self.assertEqual(self.fixture.captures, ["scan", "scan"])
        self.assertEqual(self.matcher.match.call_count, 1)
        self.fixture.interpreter.interpret.assert_not_called()
        self.assert_no_motion()
        pending = self.language.pending["record"]
        self.assertEqual(pending["saved_cup"]["id"], saved["id"])
        self.assertEqual(pending["saved_cup"]["reference_sha256"], saved["reference_sha256"])
        self.assertEqual(pending["capture_id"], self.session.inventory["capture"])
        self.assertEqual(pending["visual_matches"], [{
            "stage": "proposal", "capture_id": self.session.inventory["capture"],
            "candidate_ids": [1, 2], **visual_result()}])
        self.assertIn("coffee cup", proposal["message"])
        self.assertIn("confirm", proposal["message"])
        reference, candidates = self.matcher.match.call_args.args
        self.assertIsInstance(reference, bytes)
        self.assertEqual([c["cup_id"] for c in candidates], [1, 2])

    def test_confirm_reuses_single_pre_motion_verification_and_full_plan(self):
        saved, proposal = self.proposal()
        result = self.language.confirm(proposal["token"])
        self.assertEqual(self.fixture.captures, ["scan", "scan", "verification"])
        self.assertEqual(self.matcher.match.call_count, 2)
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.assertEqual(len(result["jobs"]), TRANSFER_JOB_COUNT)
        audit = self.session.inventory["cups"][0]["last_verification"]["named_identity"]
        self.assertEqual(audit["saved_cup_id"], saved["id"])
        self.assertEqual(audit["matched_cup_id"], 1)
        self.assertFalse(self.fixture.recovery.required())
        self.assertFalse(self.session.scan_usable)
        self.assertIsNone(self.language.pending)
        with self.assertRaisesRegex(SelectionError, "No pending"):
            self.language.confirm(proposal["token"])
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)

    def test_renumbered_fresh_ids_match_same_geometric_cup(self):
        _, proposal = self.proposal()
        extra = [detection("CUP", 40, x=450, detection_id=4),
                 detection("CUP BASE", 40, x=450, detection_id=5)]
        self.fixture.observations.append({"objects": extra + two_cups()})
        self.matcher.match.return_value = visual_result(2)
        self.language.confirm(proposal["token"])
        audit = self.session.inventory["cups"][0]["last_verification"]
        self.assertEqual(audit["matched_cup_id"], 2)
        self.assertEqual(audit["named_identity"]["matched_cup_id"], 2)
        approach = next(job for job in self.robot.jobs if job["name"] == "approach_cup")
        self.assertEqual(approach["body"]["x"], 500)
        self.assertEqual([c["cup_id"] for c in self.matcher.match.call_args.args[1]], [1, 2, 3])

    def test_visual_veto_at_unchanged_xyz_cannot_redirect_to_another_cup(self):
        _, proposal = self.proposal()
        self.matcher.match.return_value = visual_result(2)
        with self.assertRaisesRegex(SelectionError, "no longer matches the selected position"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertIsNone(self.language.pending)
        self.assertFalse(self.session.scan_usable)
        self.assertFalse(self.session.motion_uncertain)

    def test_ambiguous_absent_invalid_or_unknown_match_never_proposes(self):
        saved = self.fixture.save_reference()
        failures = [visual_result(None, "ambiguous"), visual_result(None, "not_found"),
                    visual_result(99), visual_result(True), None, {}, []]
        for failure in failures:
            with self.subTest(failure=failure):
                self.matcher.match.return_value = failure
                with self.assertRaises(SelectionError):
                    self.language.propose_saved(saved["id"])
                self.assert_no_motion()
                self.assertIsNone(self.language.pending)

    def test_ambiguous_confirmation_and_model_error_block_every_command(self):
        _, proposal = self.proposal()
        self.matcher.match.return_value = visual_result(None, "ambiguous")
        with self.assertRaisesRegex(SelectionError, "uniquely identify"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertIsNone(self.language.pending)
        saved = self.fixture.store.get("coffee cup")
        self.matcher.match.return_value = visual_result()
        proposal = self.language.propose_saved(saved["id"])
        self.matcher.match.side_effect = LLMError("Local vision model timed out")
        with self.assertRaisesRegex(LLMError, "timed out"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()

    def test_absent_match_audit_identifies_reference_and_failed_scan_without_images(self):
        saved = self.fixture.save_reference()
        self.matcher.match.return_value = {
            "status": "not_found", "cup_id": None, "reason": "No matching stripe is visible."}
        with self.assertRaisesRegex(SelectionError, "No matching stripe"):
            self.language.propose_saved(saved["id"])
        self.assert_no_motion()
        self.assertIsNone(self.language.pending)
        audit_paths = list((self.fixture.root / "audit").glob("language_*.json"))
        self.assertEqual(len(audit_paths), 1)
        audit = json.loads(audit_paths[0].read_text())
        self.assertEqual(audit["state"], "blocked")
        self.assertEqual(audit["requested_saved_cup"], saved)
        self.assertEqual(audit["capture_id"], self.session.inventory["capture"])
        self.assertNotEqual(audit["capture_id"], saved["capture_id"])
        self.assertIn("No matching stripe", audit["error"])
        self.assertIn("not_found", audit["error"])
        self.assertIn("Visual detail:", audit["error"])
        self.assertEqual(audit["visual_matches"], [{
            "stage": "proposal", "capture_id": self.session.inventory["capture"],
            "candidate_ids": [1, 2], **self.matcher.match.return_value}])
        for field in ("image_bytes", "image_jpeg_base64", "saved_cup", "action"):
            self.assertNotIn(field, audit)
        self.assertNotIn("image_bytes", audit["requested_saved_cup"])
        self.assertNotIn("image_jpeg_base64", audit["requested_saved_cup"])

    def test_contradictory_reason_cannot_upgrade_ambiguous_proposal_and_is_audited(self):
        saved = self.fixture.save_reference()
        self.matcher.match.return_value = {
            "status": "ambiguous", "cup_id": None,
            "reason": "CUP 5 matches the reference; CUP 4 lacks scribbles."}
        with self.assertRaisesRegex(SelectionError, "ambiguous.*Visual detail: CUP 5 matches"):
            self.language.propose_saved(saved["id"])
        self.assert_no_motion()
        self.assertIsNone(self.language.pending)
        self.fixture.interpreter.interpret.assert_not_called()
        audit_path, = (self.fixture.root / "audit").glob("language_*.json")
        audit = json.loads(audit_path.read_text())
        self.assertEqual(audit["state"], "blocked")
        self.assertEqual(audit["visual_matches"], [{
            "stage": "proposal", "capture_id": self.session.inventory["capture"],
            "candidate_ids": [1, 2], **self.matcher.match.return_value}])
        self.assertNotIn("action", audit)
        self.assertNotIn("image_bytes", audit_path.read_text())
        self.assertNotIn("image_jpeg_base64", audit_path.read_text())

    def test_confirmation_failure_retains_separate_fresh_visual_outcome(self):
        _, proposal = self.proposal()
        proposal_capture = self.session.inventory["capture"]
        self.matcher.match.return_value = {
            "status": "ambiguous", "cup_id": None,
            "reason": "CUP 1 matches, but another candidate remains unclear."}
        with self.assertRaisesRegex(SelectionError, "ambiguous.*Visual detail: CUP 1 matches"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertIsNone(self.language.pending)
        audit_path, = (self.fixture.root / "audit").glob("language_*.json")
        audit = json.loads(audit_path.read_text())
        self.assertEqual(audit["state"], "blocked_or_interrupted")
        first, second = audit["visual_matches"]
        self.assertEqual(first, {"stage": "proposal", "capture_id": proposal_capture,
                                 "candidate_ids": [1, 2], **visual_result()})
        self.assertEqual(second, {
            "stage": "confirmation", "capture_id": f"verification-{self.fixture.sequence}",
            "candidate_ids": [1, 2], **self.matcher.match.return_value})
        self.assertNotEqual(first["capture_id"], second["capture_id"])

    def test_invalid_visual_details_are_blocked_and_not_copied_into_audit(self):
        saved = self.fixture.save_reference()
        invalid = [dict(visual_result(), image_bytes=b"private-image"),
                   dict(visual_result(), reason="x" * 81),
                   dict(visual_result(), reason="Control\ncharacter"),
                   dict(visual_result(), status="ambiguous")]
        for response in invalid:
            with self.subTest(response=response):
                self.matcher.match.return_value = response
                with self.assertRaisesRegex(SelectionError, "invalid_response"):
                    self.language.propose_saved(saved["id"])
                self.assert_no_motion()
                self.assertIsNone(self.language.pending)
        for path in (self.fixture.root / "audit").glob("language_*.json"):
            audit = json.loads(path.read_text())
            self.assertEqual(audit["visual_matches"][0]["status"], "invalid_response")
            self.assertIsNone(audit["visual_matches"][0]["cup_id"])
            self.assertEqual(audit["visual_matches"][0]["candidate_ids"], [1, 2])
            self.assertNotIn("private-image", path.read_text())
            self.assertNotIn("image_bytes", path.read_text())
            self.assertNotIn("x" * 81, path.read_text())

    def test_match_to_blocked_cup_does_not_choose_an_available_alternative(self):
        saved = self.fixture.save_reference()
        objects = two_cups()[:-1]  # Cup 2 has no CUP BASE, but its appearance remains a candidate.
        self.fixture.observations.append({"objects": objects})
        self.matcher.match.return_value = visual_result(2)
        with self.assertRaisesRegex(SelectionError, "blocked"):
            self.language.propose_saved(saved["id"])
        self.assert_no_motion()
        self.assertEqual([c["cup_id"] for c in self.matcher.match.call_args.args[1]], [1, 2])

    def test_reference_deleted_after_proposal_invalidates_confirmation(self):
        saved, proposal = self.proposal()
        self.fixture.store.delete(saved["id"])
        with self.assertRaisesRegex(SelectionError, "no longer exists"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertEqual(self.fixture.captures, ["scan", "scan"])
        self.assertIsNone(self.language.pending)

    def test_reference_metadata_changed_after_proposal_invalidates_confirmation(self):
        _, proposal = self.proposal()
        document = json.loads(self.fixture.store.path.read_text())
        document["cups"][0]["name"] = "changed name"
        self.fixture.store.path.write_text(json.dumps(document))
        with self.assertRaisesRegex(SelectionError, "reference changed"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertEqual(self.matcher.match.call_count, 1)

    def test_reference_corrupted_after_proposal_invalidates_confirmation(self):
        _, proposal = self.proposal()
        document = json.loads(self.fixture.store.path.read_text())
        document["cups"][0]["reference_sha256"] = "0" * 64
        self.fixture.store.path.write_text(json.dumps(document))
        with self.assertRaisesRegex(SelectionError, "digest"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()

    def test_reference_changed_during_verification_is_checked_after_model_returns(self):
        saved, proposal = self.proposal()

        def change_during_match(*args):
            self.fixture.store.delete(saved["id"])
            return visual_result()

        self.matcher.match.side_effect = change_during_match
        with self.assertRaisesRegex(SelectionError, "no longer exists"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()

    def test_stop_during_proposal_match_blocks_pending_and_all_commands(self):
        saved = self.fixture.save_reference()

        def stop_during_match(*args):
            self.session.latch_stop()
            return visual_result()

        self.matcher.match.side_effect = stop_during_match
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            self.language.propose_saved(saved["id"])
        self.assert_no_motion()
        self.assertTrue(self.session.motion_uncertain)
        self.assertIsNone(self.language.pending)

    def test_stop_during_confirm_match_blocks_dispatch(self):
        _, proposal = self.proposal()

        def stop_during_match(*args):
            self.session.latch_stop()
            return visual_result()

        self.matcher.match.side_effect = stop_during_match
        with self.assertRaisesRegex(SelectionError, "Software stop"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertTrue(self.session.stop_requested)
        self.assertIsNone(self.language.pending)

    def test_model_latency_counts_toward_pre_motion_image_age(self):
        _, proposal = self.proposal()

        def slow_match(*args):
            self.fixture.now += 16
            return visual_result()

        self.matcher.match.side_effect = slow_match
        with self.assertRaisesRegex(SelectionError, "Verification image is 16.0s old"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertFalse(self.session.scan_usable)
        self.assertFalse(self.session.motion_uncertain)

    def test_movement_settings_changed_during_proposal_match_are_rejected(self):
        saved = self.fixture.save_reference()

        def change_settings(*args):
            self.session.set_speed(50)
            return visual_result()

        self.matcher.match.side_effect = change_settings
        with self.assertRaisesRegex(SelectionError, "Movement settings changed"):
            self.language.propose_saved(saved["id"])
        self.assert_no_motion()
        self.assertIsNone(self.language.pending)

    def test_missing_fresh_image_blocks_confirmation_without_fallback(self):
        _, proposal = self.proposal()
        original_capture = self.session.capture

        def missing_image(purpose):
            fresh, started = original_capture(purpose)
            Path(fresh["images"]["rgb"]).unlink()
            return fresh, started

        self.session.capture = missing_image
        with self.assertRaisesRegex(SelectionError, "missing or inaccessible"):
            self.language.confirm(proposal["token"])
        self.assert_no_motion()
        self.assertEqual(self.matcher.match.call_count, 1)

    def test_numeric_selection_uses_existing_path_without_visual_model(self):
        self.session.scan()
        proposal = self.language.propose_selection(action="goto", capture_id=self.session.inventory["capture"], cup_id=1)
        self.language.confirm(proposal["token"])
        self.assertEqual(len(self.robot.jobs), TRANSFER_JOB_COUNT)
        self.matcher.match.assert_not_called()
        self.assertEqual(self.fixture.captures, ["scan", "verification"])

    def test_batch_selection_uses_existing_path_without_visual_model(self):
        self.session.scan()
        proposal = self.language.propose_batch(capture_id=self.session.inventory["capture"])
        self.language.confirm(proposal["token"])
        self.assertEqual(len(self.robot.jobs), 2 * TRANSFER_JOB_COUNT)
        self.matcher.match.assert_not_called()
        self.assertEqual(self.fixture.captures, ["scan", "verification", "verification"])

    def test_terminal_save_and_exact_named_commands_require_confirmation(self):
        self.session.scan()
        self.assertTrue(handle_input(self.session, "save cup 1 as coffee cup", self.language))
        self.assertEqual(self.fixture.store.list()[0]["name"], "coffee cup")
        self.assert_no_motion()
        for command in ("pick saved coffee cup", "pick saved cup coffee cup", "pick coffee cup", "pick up coffee cup"):
            with self.subTest(command=command):
                self.assertTrue(handle_input(self.session, command, self.language))
                self.assertEqual(self.language.pending["record"]["saved_cup"]["name"], "coffee cup")
                self.assert_no_motion()
                self.language.cancel()
        self.fixture.interpreter.interpret.assert_not_called()

    def test_negated_or_partial_name_requests_do_not_use_exact_shortcut(self):
        self.fixture.save_reference()
        self.fixture.interpreter.interpret.return_value = {
            "action": "clarify", "selector": "none", "cup_id": None, "message": "Use the exact saved name."}
        for command in ("do not pick coffee cup", "pick coffee", "pick coffee cup tomorrow", "how do I pick coffee cup"):
            with self.subTest(command=command):
                result = self.language.propose(command)
                self.assertEqual(result["kind"], "clarify")
                self.assertIsNone(self.language.pending)
        self.matcher.match.assert_not_called()
        self.assert_no_motion()


if __name__ == "__main__":
    unittest.main()
