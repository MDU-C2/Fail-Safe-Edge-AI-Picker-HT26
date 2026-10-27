"""Offline tests: every HTTP request is mocked; no robot or camera is used."""

from datetime import datetime, timedelta, timezone
import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests

from robot_client import RobotClient, RobotCommandError


def response(payload, status=200, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = payload
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return result


def lease_response():
    return response({"success": True, "lease": {
        "active": True, "client_id": "operator",
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
    }})


def done_response():
    return response({"accepted": True, "message": "DONE"})


def move(**overrides):
    result = {"arm": "left", "wait": True, "x": 400, "y": 100, "z": 409,
              "rx": 180, "ry": 0, "rz": 180, "speed": 100, "acceleration": 50, "ramp": 50}
    result.update(overrides)
    return result


def job(name="approach cup", endpoint="/robot/rws/move", body=None):
    return {"name": name, "endpoint": endpoint, "body": move() if body is None else body}


def gripper(arm, action):
    return job(f"{arm} {action}", "/robot/rws/gripper",
               {"arm": arm, "wait": True, "action": action})


def home(arm):
    return job(f"{arm} home", "/robot/rws/home", {"arm": arm, "wait": True, "speed": 50})


class FastRenewClient(RobotClient):
    def _record_lease(self, reply, reply_headers):
        super()._record_lease(reply, reply_headers)
        self._renew_after = 0.005


class RobotClientTests(unittest.TestCase):
    def setUp(self):
        self.responses = []
        self.move_replies = []
        self.acquire_reply = None
        self.renew_reply = None
        self.release_reply = None
        self.joints_replies = []
        self.movement = None
        self.http_patch = patch("robot_client.requests.post", side_effect=self.serve)
        self.post = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.get_patch = patch("robot_client.requests.get", side_effect=self.serve_get)
        self.get = self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        self.stdout = io.StringIO()
        self.stdout_context = redirect_stdout(self.stdout)
        self.stdout_context.__enter__()
        self.addCleanup(self.stdout_context.__exit__, None, None, None)
        self.client = RobotClient("http://test/api/v1/", {"Authorization": "Bearer test"})

    def tearDown(self):
        for reply in self.responses:
            reply.close.assert_called_once()

    def serve(self, url, **kwargs):
        if url.endswith("/control/acquire"):
            result = self.acquire_reply or lease_response()
        elif url.endswith("/control/renew"):
            result = self.renew_reply or lease_response()
        elif url.endswith("/control/release"):
            result = self.release_reply or response({"success": True, "lease": {"active": False}})
        elif url.endswith(("/robot/rws/move", "/robot/rws/gripper", "/robot/rws/home")):
            if self.movement:
                self.movement()
            result = self.move_replies.pop(0) if self.move_replies else done_response()
        else:
            self.fail(f"Unexpected endpoint: {url}")
        if isinstance(result, BaseException):
            raise result
        self.responses.append(result)
        return result

    def serve_get(self, url, **kwargs):
        arm = url.split("/")[-2]
        self.assertIn(arm, ("left", "right"))
        self.assertTrue(url.endswith(f"/robot/arms/{arm}/joints"))
        result = self.joints_replies.pop(0) if self.joints_replies else response({
            "arm": arm, "joints": [0, -130, 30, 0, 40, 0],
            "arm_angle": 135 if arm == "left" else -135,
        })
        if isinstance(result, BaseException):
            raise result
        self.responses.append(result)
        return result

    def move_calls(self):
        return [call for call in self.post.call_args_list if call.args[0].endswith("/robot/rws/move")]

    def job_calls(self):
        return [call for call in self.post.call_args_list if "/robot/rws/" in call.args[0]]

    def endpoints(self):
        return [call.args[0].replace("http://test/api/v1", "") for call in self.post.call_args_list]

    def test_dry_run_prints_endpoint_json_without_http_or_callbacks(self):
        client = RobotClient("http://test/api/v1", {}, dry_run=True)
        check = Mock()
        client.execute([move(), move(x=500)], before_send=check)
        self.post.assert_not_called()
        self.get.assert_not_called()
        check.assert_not_called()
        self.assertEqual(self.stdout.getvalue().count("DRY RUN: POST /robot/rws/move"), 2)
        self.assertIn('"x": 500', self.stdout.getvalue())

    def test_two_moves_are_sequential_and_release_control(self):
        moves = [move(), move(x=500)]
        self.client.execute(moves)
        self.assertEqual(self.endpoints(), ["/control/acquire", "/robot/rws/move",
                                           "/robot/rws/move", "/control/release"])
        self.assertEqual([call.kwargs["json"] for call in self.move_calls()], moves)
        self.assertTrue(all(call.kwargs["timeout"] == 90 for call in self.move_calls()))
        self.assertFalse(self.client._owned_lease)

    def test_rejection_prevents_later_move_and_still_releases(self):
        self.move_replies = [response({"accepted": False, "message": "ERR unreachable"})]
        with self.assertRaisesRegex(RobotCommandError, "ERR unreachable"):
            self.client.execute([move(), move(x=500)])
        self.assertEqual(len(self.move_calls()), 1)
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_done_requires_true_acceptance_and_exact_completed_message(self):
        for reply in [{"accepted": False, "message": "DONE"},
                      {"accepted": True, "message": "NOT DONE"},
                      {"accepted": True, "message": "STARTED"},
                      {"accepted": 1, "message": "DONE"},
                      {"accepted": True}, {"message": "DONE"}]:
            with self.subTest(reply=reply):
                self.post.reset_mock()
                self.move_replies = [response(reply)]
                with self.assertRaises(RobotCommandError):
                    self.client.execute([move(), move(x=500)])
                self.assertEqual(len(self.move_calls()), 1)

    def test_malformed_json_stops_sequence_and_closes_response(self):
        invalid = response(None)
        invalid.json.side_effect = ValueError("not JSON")
        self.move_replies = [invalid]
        with self.assertRaisesRegex(RobotCommandError, "completion is unknown"):
            self.client.execute([move(), move(x=500)])
        self.assertEqual(len(self.move_calls()), 1)

    def test_non_object_reply_stops_sequence(self):
        self.move_replies = [response(["DONE"])]
        with self.assertRaisesRegex(RobotCommandError, "non-object"):
            self.client.execute([move(), move(x=500)])
        self.assertEqual(len(self.move_calls()), 1)

    def test_move_timeout_is_never_retried(self):
        self.move_replies = [requests.Timeout("timeout")]
        with self.assertRaisesRegex(RobotCommandError, "was not retried"):
            self.client.execute([move(), move(x=500)])
        self.assertEqual(len(self.move_calls()), 1)
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_callback_runs_after_acquisition_and_can_prevent_first_move(self):
        def check():
            self.assertEqual(self.endpoints()[-1], "/control/acquire")
            raise ValueError("cup observation is stale")
        with self.assertRaisesRegex(RobotCommandError, "cup observation is stale"):
            self.client.execute([move()], before_send=check)
        self.assertEqual(self.move_calls(), [])
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_second_callback_failure_prevents_second_move_without_retry(self):
        check = Mock(side_effect=[None, ValueError("second check failed")])
        with self.assertRaisesRegex(RobotCommandError, "second check failed"):
            self.client.execute([move(), move(x=500)], before_send=check)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(len(self.move_calls()), 1)

    def test_callback_can_replace_second_move_after_first_finishes(self):
        replacement = move(x=505, y=110)
        check = Mock(side_effect=[None, replacement])
        self.client.execute([move(), move(x=500)], before_send=check)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.move_calls()[1].kwargs["json"], replacement)

    def test_invalid_callback_replacement_prevents_second_move(self):
        for invalid in [move(wait=False), move(arm="right"), "HOME", move(x=float("nan"))]:
            with self.subTest(replacement=invalid):
                self.post.reset_mock()
                check = Mock(side_effect=[None, invalid])
                with self.assertRaisesRegex(RobotCommandError, "Pre-command check failed"):
                    self.client.execute([move(), move(x=500)], before_send=check)
                self.assertEqual(len(self.move_calls()), 1)

    def test_initial_acquisition_failure_does_not_send_or_release(self):
        self.acquire_reply = response({}, status=409)
        with self.assertRaises(RobotCommandError):
            self.client.execute([move()])
        self.assertEqual(self.endpoints(), ["/control/acquire"])

    def test_repeated_execution_reacquires_after_successful_release(self):
        self.client.execute([move()])
        self.client.execute([move(x=500)])
        self.assertEqual(self.endpoints(), ["/control/acquire", "/robot/rws/move", "/control/release"] * 2)

    def test_false_lease_success_never_sends_move(self):
        self.acquire_reply = response({"success": False, "lease": {"active": True}})
        with self.assertRaisesRegex(RobotCommandError, "active control lease"):
            self.client.execute([move()])
        self.assertEqual(self.move_calls(), [])

    def test_invalid_expiry_after_confirmed_acquisition_still_releases(self):
        self.acquire_reply = response({"success": True, "lease": {"active": True, "expires_at": "invalid"}})
        with self.assertRaisesRegex(RobotCommandError, "invalid lease expiry"):
            self.client.execute([move()])
        self.assertEqual(self.endpoints(), ["/control/acquire", "/control/release"])

    def test_background_renews_while_move_waits_and_stops_before_release(self):
        client = FastRenewClient("http://test/api/v1", {})
        def wait_for_renewal():
            deadline = time.monotonic() + 1
            while "/control/renew" not in self.endpoints() and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertIn("/control/renew", self.endpoints())
        self.movement = wait_for_renewal
        client.execute([move()])
        self.assertEqual(self.endpoints()[-1], "/control/release")
        count = self.post.call_count
        time.sleep(0.02)
        self.assertEqual(self.post.call_count, count)

    def test_renewal_failure_during_move_prevents_next_move(self):
        client = FastRenewClient("http://test/api/v1", {})
        self.renew_reply = response({}, status=409)
        def wait_for_failure():
            deadline = time.monotonic() + 1
            while client._lease_error is None and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertIsNotNone(client._lease_error)
        self.movement = wait_for_failure
        with self.assertRaisesRegex(RobotCommandError, "already dispatched movement"):
            client.execute([move(), move(x=500)])
        self.assertEqual(len(self.move_calls()), 1)
        self.assertFalse(client._owned_lease)
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_expiry_during_callback_prevents_dispatch(self):
        def check():
            self.client._lease_deadline = time.monotonic() - 1
        with self.assertRaisesRegex(RobotCommandError, "lease expired"):
            self.client.execute([move()], before_send=check)
        self.assertEqual(self.move_calls(), [])

    def test_lease_uses_gateway_clock_when_clocks_differ(self):
        self.acquire_reply = response({"success": True, "lease": {
            "active": True, "expires_at": "2020-01-01T00:00:05Z",
        }}, headers={"date": "Wed, 01 Jan 2020 00:00:00 GMT"})
        self.client.execute([move()])
        self.assertEqual(len(self.move_calls()), 1)

    def test_release_failure_does_not_mask_movement_failure(self):
        self.move_replies = [response({"accepted": False, "message": "ERR move failed"})]
        self.release_reply = requests.Timeout("release timeout")
        with self.assertRaisesRegex(RobotCommandError, "ERR move failed"):
            self.client.execute([move()])
        self.assertIn("Warning: control release failed", self.stdout.getvalue())
        self.assertFalse(self.client._owned_lease)

    def test_failed_release_after_success_warns_and_clears_local_ownership(self):
        self.release_reply = response({}, status=503)
        self.client.execute([move()])
        self.assertIn("Warning: control release failed", self.stdout.getvalue())
        self.assertFalse(self.client._owned_lease)

    def test_keyboard_interrupt_still_releases_and_propagates(self):
        with self.assertRaises(KeyboardInterrupt):
            self.client.execute([move()], before_send=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(self.endpoints(), ["/control/acquire", "/control/release"])

    def test_rejects_legacy_or_invalid_moves_before_acquiring(self):
        invalid = ["GOTO,400,100,200", "HOME", "PICK,400,100,75",
                   move(x=float("nan")), move(z=float("inf")), move(rx=float("-inf")),
                   move(x=True), move(x=10**1000), move(wait=False), move(wait=1), move(arm="right"),
                   move(action="grip"), move(command="HOME"), move(rx=None),
                   move(speed=0), move(speed=1501), move(acceleration=19), move(ramp=101),
                   {key: value for key, value in move().items() if key != "rz"}]
        for invalid_move in invalid:
            with self.subTest(move=invalid_move):
                with self.assertRaises(RobotCommandError):
                    self.client.execute([invalid_move])
        self.post.assert_not_called()

    def test_optional_motion_settings_can_be_omitted(self):
        minimal = {key: value for key, value in move().items()
                   if key not in ("speed", "acceleration", "ramp")}
        self.client.execute([minimal])
        self.assertEqual(self.move_calls()[0].kwargs["json"], minimal)

    def test_empty_list_does_not_acquire(self):
        self.client.execute([])
        self.post.assert_not_called()

    def test_validates_entire_sequence_before_dispatch(self):
        with self.assertRaises(RobotCommandError):
            self.client.execute([move(), move(wait=False)])
        self.post.assert_not_called()

    def test_concurrent_execution_is_rejected_without_additional_requests(self):
        self.client._execution_lock.acquire()
        try:
            with self.assertRaisesRegex(RobotCommandError, "already executing"):
                self.client.execute([move()])
        finally:
            self.client._execution_lock.release()
        self.post.assert_not_called()

    def test_named_jobs_support_both_arms_and_all_endpoints_in_exact_order(self):
        jobs = [gripper("left", "open"), job(), gripper("left", "grip"),
                job("right approach", body=move(arm="right")), gripper("right", "grip"),
                gripper("left", "open"), home("left"), gripper("right", "open"), home("right")]
        self.client.execute_jobs(jobs)
        self.assertEqual(self.endpoints(), ["/control/acquire"] +
                         [item["endpoint"] for item in jobs] + ["/control/release"])
        self.assertEqual([call.kwargs["json"] for call in self.job_calls()],
                         [item["body"] for item in jobs])
        self.assertTrue(all(call.kwargs["timeout"] == 90 for call in self.job_calls()))
        self.assertEqual([call.args[0] for call in self.get.call_args_list], [
            "http://test/api/v1/robot/arms/left/joints", "http://test/api/v1/robot/arms/right/joints",
        ])
        self.assertTrue(all(call.kwargs == {"headers": self.client.headers, "timeout": 5}
                            for call in self.get.call_args_list))

    def test_right_grip_failure_prevents_left_release_and_all_recovery(self):
        self.move_replies = [done_response(), response({"accepted": False, "message": "ERR grip"})]
        jobs = [gripper("left", "grip"), gripper("right", "grip"),
                gripper("left", "open"), home("right"), home("left")]
        completed = Mock()
        with self.assertRaisesRegex(RobotCommandError, "right grip.*ERR grip"):
            self.client.execute_jobs(jobs, after_done=completed)
        self.assertEqual([call.kwargs["json"] for call in self.job_calls()],
                         [item["body"] for item in jobs[:2]])
        completed.assert_called_once_with(0, jobs[0])
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_gripper_started_is_not_completion_and_prevents_release(self):
        self.move_replies = [response({"accepted": True, "message": "STARTED"})]
        completed = Mock()
        with self.assertRaisesRegex(RobotCommandError, "STARTED"):
            self.client.execute_jobs([gripper("right", "grip"), gripper("left", "open")],
                                     after_done=completed)
        self.assertEqual(len(self.job_calls()), 1)
        completed.assert_not_called()

    def test_gripper_timeout_is_not_retried_or_followed_by_home(self):
        self.move_replies = [requests.Timeout("gripper timeout")]
        with self.assertRaisesRegex(RobotCommandError, "was not retried"):
            self.client.execute_jobs([gripper("right", "grip"), home("right")])
        self.assertEqual(len(self.job_calls()), 1)
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_named_job_dry_run_prints_all_steps_without_http_or_hooks(self):
        client = RobotClient("http://test/api/v1", {}, dry_run=True)
        before, after = Mock(), Mock()
        jobs = [job(), gripper("right", "grip"), home("left"), home("right")]
        client.execute_jobs(jobs, before_send=before, after_done=after)
        self.post.assert_not_called()
        self.get.assert_not_called()
        before.assert_not_called()
        after.assert_not_called()
        self.assertEqual(self.stdout.getvalue().count("DRY RUN: POST"), len(jobs))
        for item in jobs:
            self.assertIn(item["name"], self.stdout.getvalue())
            self.assertIn(item["endpoint"], self.stdout.getvalue())

    def test_entire_named_plan_is_validated_before_acquisition(self):
        invalid_jobs = [None, "HOME", {}, {"name": "", "endpoint": "/robot/rws/home", "body": {}},
                        {**home("left"), "name": "  "}, {**home("left"), "name": 1},
                        {**home("left"), "extra": 1}, {**home("left"), "endpoint": "/robot/command"},
                        {**home("left"), "endpoint": []}, {**home("left"), "body": None},
                        job(body=move(arm="middle")), job(body=move(wait=1)),
                        job(body=move(x=float("nan"))), job(body=move(z=10**1000)),
                        job(body=move(command="HOME")),
                        job(endpoint="/robot/rws/home", body=move()),
                        job(endpoint="/robot/rws/home", body={"arm": "left", "wait": True, "speed": 0}),
                        job(endpoint="/robot/rws/home", body={"arm": "left", "wait": False}),
                        job(endpoint="/robot/rws/home", body={"arm": "left", "wait": True, "ramp": float("inf")}),
                        job(endpoint="/robot/rws/gripper", body={"arm": "right", "wait": True, "action": "release"}),
                        job(endpoint="/robot/rws/gripper", body={"arm": "right", "wait": False, "action": "grip"}),
                        job(endpoint="/robot/rws/gripper", body={"arm": "right", "wait": 1, "action": "grip"}),
                        job(endpoint="/robot/rws/gripper", body={"arm": "right", "wait": True, "action": "grip", "speed": 1}),
                        job(endpoint="/robot/rws/gripper", body={"arm": "left", "wait": True})]
        for invalid in invalid_jobs:
            with self.subTest(invalid=invalid):
                with self.assertRaises(RobotCommandError):
                    self.client.execute_jobs([job(), invalid])
        self.post.assert_not_called()

    def test_named_jobs_require_list_and_empty_plan_does_nothing(self):
        for invalid in (None, job(), "HOME", (job(),)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RobotCommandError):
                    self.client.execute_jobs(invalid)
        self.client.execute_jobs([])
        self.post.assert_not_called()

    def test_named_job_hooks_run_in_order_and_record_replacement(self):
        jobs = [job(), gripper("right", "grip")]
        replacement = job("updated approach", body=move(x=450))
        events = []

        def before(index, item):
            events.append(("before", index))
            self.assertEqual(len(self.job_calls()), index)
            self.assertEqual(item, jobs[index])
            return replacement if index == 0 else None

        def after(index, item):
            events.append(("after", index))
            self.assertEqual(len(self.job_calls()), index + 1)
            self.assertEqual(item, replacement if index == 0 else jobs[index])

        self.client.execute_jobs(jobs, before_send=before, after_done=after)
        self.assertEqual(events, [("before", 0), ("after", 0), ("before", 1), ("after", 1)])
        self.assertEqual(self.job_calls()[0].kwargs["json"], replacement["body"])

    def test_invalid_named_replacement_blocks_dispatch_and_releases(self):
        before = Mock(side_effect=[None, {**gripper("right", "grip"), "endpoint": "/robot/command"}])
        with self.assertRaisesRegex(RobotCommandError, "Pre-command check failed"):
            self.client.execute_jobs([job(), gripper("right", "grip")], before_send=before)
        self.assertEqual(len(self.job_calls()), 1)
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_completion_recording_error_prevents_next_job_and_releases(self):
        after = Mock(side_effect=OSError("cannot save progress"))
        with self.assertRaisesRegex(RobotCommandError, "reported DONE.*cannot save progress"):
            self.client.execute_jobs([gripper("right", "grip"), gripper("left", "open")],
                                     after_done=after)
        self.assertEqual(len(self.job_calls()), 1)
        after.assert_called_once()
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_hooks_cannot_mutate_other_already_validated_jobs(self):
        jobs = [job(), gripper("right", "grip")]
        original_second = dict(jobs[1]["body"])

        def before(index, item):
            item["body"]["arm"] = "invalid"
            jobs[1]["body"]["action"] = "invalid"

        self.client.execute_jobs(jobs, before_send=before)
        self.assertEqual(self.job_calls()[0].kwargs["json"]["arm"], "left")
        self.assertEqual(self.job_calls()[1].kwargs["json"], original_second)

    def test_completed_step_is_recorded_even_if_lease_expired_during_job(self):
        def expire():
            self.client._lease_deadline = time.monotonic() - 1
        self.movement = expire
        completed = Mock()
        jobs = [gripper("right", "grip"), gripper("left", "open")]
        with self.assertRaisesRegex(RobotCommandError, "lease expired"):
            self.client.execute_jobs(jobs, after_done=completed)
        completed.assert_called_once_with(0, jobs[0])
        self.assertEqual(len(self.job_calls()), 1)
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_home_done_with_wrong_joint_prevents_next_job_and_completion_record(self):
        self.joints_replies = [response({"arm": "left", "joints": [0, -100, 30, 0, 40, 0],
                                        "arm_angle": 135})]
        completed = Mock()
        with self.assertRaisesRegex(RobotCommandError, "home pose was not confirmed.*joint 2"):
            self.client.execute_jobs([home("left"), gripper("right", "open")], after_done=completed)
        completed.assert_not_called()
        self.assertEqual(len(self.job_calls()), 1)
        self.get.assert_called_once()
        self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_home_verification_rejects_malformed_and_wrong_arm_measurements(self):
        base = {"arm": "right", "joints": [0, -130, 30, 0, 40, 0], "arm_angle": -135}
        bad = [None, [], {}, {**base, "arm": "left"}, {**base, "joints": [0, -130]},
               {**base, "joints": "0,-130,30,0,40,0"},
               {**base, "joints": [True, -130, 30, 0, 40, 0]},
               {**base, "joints": [float("nan"), -130, 30, 0, 40, 0]},
               {**base, "joints": [10**1000, -130, 30, 0, 40, 0]},
               {**base, "arm_angle": 135}, {**base, "arm_angle": float("inf")},
               {**base, "arm_angle": False}, {**base, "arm_angle": "-135"},
               {key: value for key, value in base.items() if key != "arm_angle"}]
        for payload in bad:
            with self.subTest(payload=payload):
                self.post.reset_mock()
                self.joints_replies = [response(payload)]
                with self.assertRaises(RobotCommandError):
                    self.client.execute_jobs([home("right"), gripper("right", "open")])
                self.assertEqual(len(self.job_calls()), 1)
                self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_home_verification_accepts_measurements_within_one_degree(self):
        self.joints_replies = [response({"arm": "left", "joints": [1, -131, 30.5, 0, 39.5, 0],
                                        "arm_angle": 134})]
        completed = Mock()
        self.client.execute_jobs([home("left")], after_done=completed)
        completed.assert_called_once_with(0, home("left"))

    def test_home_read_failure_is_not_retried_and_prevents_next_job(self):
        malformed = response(None)
        malformed.json.side_effect = ValueError("invalid JSON")
        for reply in [requests.Timeout("read timeout"), response({}, status=503), malformed]:
            with self.subTest(reply=reply):
                self.post.reset_mock()
                self.get.reset_mock()
                self.joints_replies = [reply]
                completed = Mock()
                with self.assertRaisesRegex(RobotCommandError, "Home verification.*failed"):
                    self.client.execute_jobs([home("right"), home("left")], after_done=completed)
                self.assertEqual(len(self.job_calls()), 1)
                self.get.assert_called_once()
                completed.assert_not_called()
                self.assertEqual(self.endpoints()[-1], "/control/release")

    def test_home_confirmation_happens_before_completion_hook_and_next_job(self):
        def after(index, item):
            self.assertEqual(self.get.call_count, index + 1)
            self.assertEqual(len(self.job_calls()), index + 1)
        self.client.execute_jobs([home("left"), home("right")], after_done=after)


if __name__ == "__main__":
    unittest.main()
