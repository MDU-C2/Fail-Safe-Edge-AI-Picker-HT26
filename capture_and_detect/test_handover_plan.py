"""Offline checks of fixed-height picking and handover ordering/geometry."""

import copy
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from cup_inventory import FLANGE_MAX, Z_MIN, TRAVEL_Z, SelectionError, hover_moves
from handover_plan import (
    DEFAULT_SPEED, HOME_SPEED, PICK_SPEED, PICK_Z, CUP_Z_MIN, CUP_Z_MAX, FLIP_ACCELERATION, FLIP_SPEED, PLACE_ABOVE,
    handover_jobs, pick_position_problem,
)


def configuration_constant(node, values):
    """Read numeric configuration arithmetic without executing upstream code."""
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(configuration_constant(item, values) for item in node.elts)
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Subscript):
        value = configuration_constant(node.value, values)
        index = configuration_constant(node.slice, values)
        if isinstance(value, tuple) and type(index) is int:
            return value[index]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = configuration_constant(node.operand, values)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        left = configuration_constant(node.left, values)
        right = configuration_constant(node.right, values)
        return left + right if isinstance(node.op, ast.Add) else left - right
    raise ValueError("Not supported configuration arithmetic")


class HandoverPlanTests(unittest.TestCase):
    def setUp(self):
        self.cup = {"x": 500, "y": 100, "z": 50, "selectable": True, "blocked_reason": None}
        self.place = {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}

    def plan(self, **kwargs):
        kwargs.setdefault("place", self.place)
        return handover_jobs(self.cup, **kwargs)

    def test_serial_interlocks_require_holding_and_clearing_before_releasing(self):
        jobs = self.plan()
        names = [job["name"] for job in jobs]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[:7], ["right_home_start", "left_open", "left_ready", "approach_cup",
                                    "descend_to_cup", "grip_cup", "lift_cup"])
        for first, second in (("lift_cup", "left_handover"), ("left_handover", "right_via"),
                              ("right_open", "right_take"), ("right_beside", "right_below"),
                              ("right_below", "right_take"), ("right_take", "right_grip"),
                              ("right_grip", "left_release"), ("left_release", "left_clear"),
                              ("left_clear", "right_lower"), ("right_lower", "right_turn_half"),
                              ("right_turn_half", "right_retreat"),
                              ("right_retreat", "left_home"), ("right_place", "right_release"),
                              ("right_release", "right_clear")):
            with self.subTest(first=first, second=second):
                self.assertLess(names.index(first), names.index(second))
        self.assertEqual(names[-1], "right_home")
        self.assertTrue(all(job["body"]["wait"] is True for job in jobs))
        by_name = {job["name"]: job for job in jobs}
        for name, arm, action in (("left_open", "left", "open"), ("grip_cup", "left", "grip"),
                                  ("right_open", "right", "open"), ("right_grip", "right", "grip"),
                                  ("left_release", "left", "open"), ("right_release", "right", "open")):
            self.assertEqual(by_name[name]["endpoint"], "/robot/rws/gripper")
            self.assertEqual(by_name[name]["body"], {"arm": arm, "action": action, "wait": True})

    def test_original_tuned_flange_poses_and_orientations_are_preserved(self):
        bodies = {job["name"]: job["body"] for job in self.plan()}
        expected = {
            "left_ready": ("left", (400, 100, 450), (180, 0, 180)),
            "left_handover": ("left", (350, -100, 655), (180, 0, 180)),
            "left_clear": ("left", (350, -100, 690), (180, 0, 180)),
            "right_via": ("right", (300, -300, 250), (0, 0, 0)),
            "right_beside": ("right", (330, -235, 281), (0, 0, 0)),
            "right_below": ("right", (330, -115, 281), (0, 0, 0)),
            "right_take": ("right", (330, -115, 316), (0, 0, 0)),
            "right_lower": ("right", (330, -115, 281), (0, 0, 0)),
            "right_turn_half": ("right", (375, -157.5, 295.5), (90, 0, 0)),
            "right_retreat": ("right", (420, -200, 310), (180, 0, 0)),
            "right_place": ("right", (420, -200, 230), (180, 0, 0)),
            "right_clear": ("right", (420, -200, 310), (180, 0, 0)),
        }
        for name, (arm, xyz, rotation) in expected.items():
            with self.subTest(name=name):
                body = bodies[name]
                self.assertEqual(body["arm"], arm)
                self.assertEqual(tuple(body[a] for a in ("x", "y", "z")), xyz)
                self.assertEqual(tuple(body[a] for a in ("rx", "ry", "rz")), rotation)

    def test_calibration_xy_is_applied_once_only_to_cup_moves_and_z_is_fixed(self):
        original = copy.deepcopy(self.cup)
        baseline = {job["name"]: job for job in self.plan()}
        for z_offset in (-1000, 0, 40, 1000):
            with self.subTest(z_offset=z_offset):
                jobs = self.plan(tag_offset=(-115, 25, z_offset))
                for job in jobs:
                    name, body = job["name"], job["body"]
                    if name in ("approach_cup", "descend_to_cup", "lift_cup"):
                        self.assertEqual((body["x"], body["y"]), (385, 125))
                        self.assertEqual(body["z"], 229 if name == "descend_to_cup" else 334)
                        self.assertEqual(tuple(body[a] for a in ("rx", "ry", "rz")), (180, 0, 180))
                    else:
                        self.assertEqual(job, baseline[name])
        self.assertEqual(self.cup, original)

    def test_every_move_and_home_has_explicit_selected_speed(self):
        for speed in (1, 50, 120, 1500):
            for job in self.plan(speed=speed):
                with self.subTest(speed=speed, job=job["name"]):
                    self.assertIn(job["endpoint"], ("/robot/rws/move", "/robot/rws/home", "/robot/rws/gripper"))
                    self.assertEqual(set(job), {"name", "endpoint", "body"})
                    body = job["body"]
                    if job["endpoint"].endswith(("/move", "/home")):
                        expected = ((min(speed, FLIP_SPEED), FLIP_ACCELERATION, 100)
                                    if job["name"] in ("right_turn_half", "right_retreat")
                                    else (min(speed, HOME_SPEED), 100, 100)
                                    if job["endpoint"].endswith("/home")
                                    else (min(speed, PICK_SPEED), 100, 100)
                                    if job["name"] in ("descend_to_cup", "lift_cup", "left_handover")
                                    else (speed, 100, 100))
                        self.assertEqual((body["speed"], body["acceleration"], body["ramp"]), expected)
                    if job["endpoint"].endswith("/home"):
                        self.assertEqual(set(body), {"arm", "wait", "speed", "acceleration", "ramp"})

    def test_outside_existing_saved_or_corrected_workspace_is_blocked(self):
        for cup, offset in ((dict(self.cup, y=-627.1), (0, 0, 0)),
                            (dict(self.cup, y=708.2), (0, 0, 0)),
                            (dict(self.cup, x=349), (0, 0, 0)),
                            (dict(self.cup, x=801), (0, 0, 0)),
                            (self.cup, (-321, 0, 0)), (self.cup, (0, 401, 0))):
            with self.subTest(cup=cup, offset=offset), self.assertRaisesRegex(SelectionError, "workspace"):
                handover_jobs(cup, place=self.place, tag_offset=offset)

    def test_every_fixed_move_target_is_checked_before_any_plan_is_returned(self):
        for constant, value in (("HANDOVER_L", (350, -100, 701)),
                                ("LEFT_LIFT", 46), ("R_VIA", (179, -250, 200)),
                                ("PLACE_ABOVE", 471),
                                ("PICK_Z", -110)):
            with self.subTest(constant=constant), patch("handover_plan." + constant, value):
                with self.assertRaisesRegex(SelectionError, "workspace"):
                    self.plan()

    def test_fixed_height_pick_requires_table_height_without_changing_hover_rules(self):
        for height in (-55, -30.1, 150.1, 174.9):
            cup = dict(self.cup, z=height)
            with self.subTest(height=height):
                self.assertIsNotNone(pick_position_problem(cup))
                with self.assertRaisesRegex(SelectionError, "-30..150"):
                    handover_jobs(cup, place=self.place)
                self.assertEqual(len(hover_moves(cup)), 2)
        for height in (-30, -15, 0, 50, 150):
            self.assertIsNone(pick_position_problem(dict(self.cup, z=height)))
            self.assertEqual(len(handover_jobs(dict(self.cup, z=height), place=self.place)), 24)

    def test_ordinary_clearance_applies_except_to_explicit_pick_and_place(self):
        with patch("handover_plan.R_VIA", (300, -300, 198.9)):
            with self.assertRaisesRegex(SelectionError, "ordinary motion clearance"):
                self.plan()
        with patch("handover_plan.PICK_Z", 30):
            pick = next(job for job in self.plan() if job["name"] == "descend_to_cup")
            self.assertEqual(pick["body"]["z"], 189)
        jobs = self.plan(place=dict(self.place, z=119))
        by_name = {job["name"]: job["body"] for job in jobs}
        self.assertEqual(by_name["right_place"]["z"], 119)
        self.assertEqual(by_name["right_clear"]["z"], 199)
        with self.assertRaisesRegex(SelectionError, "ordinary motion clearance"):
            self.plan(place=dict(self.place, z=118.9))

    def test_configuration_reader_rejects_calls_and_attribute_access(self):
        for expression in ("run_hardware()", "robot.move", "[x for x in items]"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                configuration_constant(ast.parse(expression, mode="eval").body, {})

    def test_invalid_inputs_or_blocked_selection_never_produce_a_plan(self):
        for axis in ("x", "y", "z"):
            for value in (None, float("nan"), float("inf"), True, "50"):
                with self.subTest(axis=axis, value=value), self.assertRaises(SelectionError):
                    handover_jobs(dict(self.cup, **{axis: value}), place=self.place)
        for offset in (None, (), (0, 0), (0, 0, float("nan")), (True, 0, 0)):
            with self.subTest(offset=offset), self.assertRaises(SelectionError):
                self.plan(tag_offset=offset)
        for speed in (0, 1501, None, float("nan"), float("inf"), True, "50"):
            with self.subTest(speed=speed), self.assertRaises(SelectionError):
                self.plan(speed=speed)
        for cup in (dict(self.cup, selectable=False), dict(self.cup, blocked_reason="ambiguous base")):
            with self.subTest(cup=cup), self.assertRaises(SelectionError):
                handover_jobs(cup, place=self.place)

    def test_place_is_required_and_controls_only_placing_targets(self):
        with self.assertRaises(TypeError):
            handover_jobs(self.cup)
        alternative = dict(self.place, id=10, x=480, y=-360)
        original = copy.deepcopy(alternative)
        jobs = {job["name"]: job for job in self.plan(place=alternative)}
        baseline = {job["name"]: job for job in self.plan()}
        expected = {"right_turn_half": (405, -237.5, 295.5),
                    "right_retreat": (480, -360, 310),
                    "right_place": (480, -360, 230),
                    "right_clear": (480, -360, 310)}
        for name, job in jobs.items():
            if name in expected:
                self.assertEqual(tuple(job["body"][axis] for axis in ("x", "y", "z")), expected[name])
            else:
                self.assertEqual(job, baseline[name])
        self.assertEqual(alternative, original)

    def test_invalid_place_never_returns_a_plan(self):
        for place in (None, (), {}, dict(self.place, x=float("nan")),
                      dict(self.place, y=501), dict(self.place, z=621)):
            with self.subTest(place=place), self.assertRaises(SelectionError):
                self.plan(place=place)

    def test_defaults_and_waypoints_match_original_program_without_importing_it(self):
        # Parse numeric configuration only: importing the original runs hardware.
        source = Path(__file__).with_name("capture_and_detect.py").read_text()
        tree = ast.parse(source)
        original = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                try:
                    value = configuration_constant(node.value, original)
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    original[target.id] = value
                elif (isinstance(target, ast.Tuple) and isinstance(value, tuple)
                      and len(target.elts) == len(value)
                      and all(isinstance(item, ast.Name) for item in target.elts)):
                    original.update((item.id, part) for item, part in zip(target.elts, value))
        self.assertEqual(DEFAULT_SPEED, original["SPEED"])
        self.assertEqual(HOME_SPEED, original["HOME_SPEED"])
        self.assertEqual(PICK_SPEED, original["PICK_SPEED"])
        self.assertEqual(PICK_Z, original["PICK_Z"])
        self.assertEqual((CUP_Z_MIN, CUP_Z_MAX), (original["CUP_Z_MIN"], original["CUP_Z_MAX"]))
        self.assertEqual(TRAVEL_Z, original["TRAVEL_Z"])
        self.assertEqual(Z_MIN, original["Z_MIN"])
        self.assertEqual(FLANGE_MAX[2], original["GATEWAY_Z_MAX"])
        self.assertEqual(FLIP_SPEED, original["FLIP_SPEED"])
        self.assertEqual(FLIP_ACCELERATION, original["FLIP_ACCELERATION"])
        self.assertEqual(PLACE_ABOVE, original["PLACE_ABOVE"])
        for location in ((420, -200, 230), (480, -360, 230), (640, -280, 230)):
            with self.subTest(location=location):
                place = dict(self.place, **dict(zip(("x", "y", "z"), location)))
                bodies = {job["name"]: job["body"] for job in self.plan(place=place)}
                take = original["R_TAKE"]
                below = (take[0], take[1], take[2] - 35)
                above = (location[0], location[1], location[2] + original["PLACE_ABOVE"])
                half = tuple((a + b) / 2 for a, b in zip(below, above))
                for name, xyz, rotation in (
                        ("left_ready", original["LEFT_READY"], original["LEFT_DOWN"]),
                        ("left_handover", original["HANDOVER_L"], original["LEFT_DOWN"]),
                        ("right_via", original["R_VIA"], original["RIGHT_UP"]),
                        ("right_below", below, original["RIGHT_UP"]),
                        ("right_take", take, original["RIGHT_UP"]),
                        ("right_turn_half", half, original["RIGHT_SIDE"]),
                        ("right_retreat", above, original["RIGHT_DOWN"]),
                        ("right_place", location, original["RIGHT_DOWN"])):
                    body = bodies[name]
                    self.assertEqual(tuple(body[key] for key in ("x", "y", "z")), xyz)
                    self.assertEqual(tuple(body[key] for key in ("rx", "ry", "rz")), rotation)
                for name, body in bodies.items():
                    if "speed" in body:
                        if name in ("right_turn_half", "right_retreat"):
                            self.assertEqual(body["speed"], original["FLIP_SPEED"])
                            self.assertEqual(body["acceleration"], original["FLIP_ACCELERATION"])
                        elif name in ("descend_to_cup", "lift_cup", "left_handover"):
                            self.assertEqual(body["speed"], original["PICK_SPEED"])
                            self.assertEqual(body["acceleration"], original["ACCELERATION"])
                        elif name in ("right_home_start", "left_home", "right_home"):
                            self.assertEqual(body["speed"], original["HOME_SPEED"])
                            self.assertEqual(body["acceleration"], original["ACCELERATION"])
                        else:
                            self.assertEqual(body["speed"], original["SPEED"])
                            self.assertEqual(body["acceleration"], original["ACCELERATION"])
                        self.assertEqual(body["ramp"], original["RAMP"])


if __name__ == "__main__":
    unittest.main()
