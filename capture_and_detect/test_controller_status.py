"""Offline native-controller status tests; every HTTP operation is mocked."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from requests.auth import HTTPDigestAuth

from controller_status import (MAX_PROFILE_BYTES, MAX_RESPONSE_BYTES,
                               NativeControllerStatus, load_controller_status_profile)


API = "http://gateway.test:8000/api/v1"
ORIGIN = "http://controller.test"


def profile(**changes):
    return {"schema_version": 1, "gateway_api": API, "controller_url": ORIGIN, **changes}


def gateway_arm(arm):
    return {"arm": arm, "mech_unit": "ROB_R" if arm == "right" else "ROB_L",
            "joints": [0, -130, 30, 0, 40, 0], "arm_angle": -135 if arm == "right" else 135}


def native_arm(arm):
    gateway = gateway_arm(arm)
    return {**{f"rax_{i}": str(value) for i, value in enumerate(gateway["joints"], 1)},
            "eax_a": str(gateway["arm_angle"])}


def reply(entry=None, *, payload=None, status=200):
    data = {"_embedded": {"_state": [entry]}} if payload is None else payload
    response = Mock()
    response.status_code = status
    response.content = json.dumps(data).encode()
    response.json.return_value = data
    response.iter_content.side_effect = lambda **kwargs: iter([response.content])
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError("secret controller response")
    return response


class ControllerProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "controller.json"
        self.network = patch("controller_status.requests.get").start()
        self.addCleanup(patch.stopall)

    def write(self, value):
        self.path.write_text(json.dumps(value))

    def test_missing_and_exact_gateway_mismatch_do_not_connect(self):
        self.assertIsNone(load_controller_status_profile(self.path, API))
        self.write(profile(schema_version=900, controller_url="not a URL"))
        for api in (API + "/", API.replace("gateway.test", "other.test"), API.upper()):
            with self.subTest(api=api):
                self.assertIsNone(load_controller_status_profile(self.path, api))
        self.network.assert_not_called()

    def test_matching_profile_loads_without_network_and_uses_environment_credentials(self):
        self.write(profile())
        with patch.dict(os.environ, {"GATEWAY_RWS_USER": "operator", "GATEWAY_RWS_PASSWORD": "private"}):
            reader = load_controller_status_profile(self.path, API)
        self.assertIsInstance(reader, NativeControllerStatus)
        self.assertEqual(reader.controller_url, ORIGIN)
        self.assertEqual(reader._auth.username, "operator")
        self.assertEqual(reader._auth.password, "private")
        self.assertNotIn("private", repr(reader))
        self.network.assert_not_called()

    def test_defaults_match_project_abb_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            reader = NativeControllerStatus(ORIGIN)
        self.assertEqual((reader._auth.username, reader._auth.password), ("Default User", "robotics"))

    def test_matching_malformed_profile_rejected_without_network(self):
        for value in ([], None, profile(schema_version=True), profile(schema_version=2),
                      profile(schema_version=1.0), profile(controller_url=None),
                      profile(password="do not store this here"),
                      {"gateway_api": API, "schema_version": 1}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    load_controller_status_profile(self.path, API)
        self.path.write_text("{\"gateway_api\":")
        with self.assertRaises(ValueError):
            load_controller_status_profile(self.path, API)
        self.path.write_bytes(b" " * (MAX_PROFILE_BYTES + 1))
        with self.assertRaises(ValueError):
            load_controller_status_profile(self.path, API)
        self.network.assert_not_called()

    def test_conflicting_duplicate_profile_fields_rejected(self):
        self.path.write_text('{"schema_version":1,"gateway_api":"' + API + '",'
                             '"controller_url":"http://wrong.test",'
                             '"controller_url":"http://controller.test"}')
        with self.assertRaises(ValueError):
            load_controller_status_profile(self.path, API)
        self.network.assert_not_called()

    def test_controller_must_be_http_origin_without_credentials_or_suffix(self):
        values = [None, 2, "", "ftp://robot", "http://user:private@robot", "http://robot/",
                  "http://robot/path", "http://robot?x=1", "http://robot#part", "http://robot?",
                  "http://robot#", "http://robot:99999", "http://robot:0", "http://robot:bad",
                  "http://robot\\other", "http://robot\n", " http://robot", "http://"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                NativeControllerStatus(value)
        for value in ("https://robot:443", "http://[::1]:80", ORIGIN):
            self.assertEqual(NativeControllerStatus(value).controller_url, value)
        self.network.assert_not_called()


class NativeControllerStatusTests(unittest.TestCase):
    def setUp(self):
        self.reader = NativeControllerStatus(ORIGIN)
        self.gateway = Mock(side_effect=lambda endpoint: gateway_arm(endpoint.split("/")[-2]))
        self.native = patch("controller_status.requests.get").start()
        self.post = patch("controller_status.requests.post").start()
        self.addCleanup(patch.stopall)
        self.responses = []

    def queue(self, right=None, left=None, collision="INIT"):
        self.responses = [reply(native_arm("right") if right is None else right),
                          reply(native_arm("left") if left is None else left),
                          reply({"coldetstate ": collision})]
        self.native.side_effect = self.responses

    def test_fresh_two_arm_agreement_before_collision_and_get_only(self):
        timeline = []
        self.queue()
        responses = iter(self.responses)
        self.native.side_effect = lambda url, **kwargs: (timeline.append(url), next(responses))[1]
        self.gateway.side_effect = lambda endpoint: (timeline.append(endpoint),
                                                    gateway_arm(endpoint.split("/")[-2]))[1]
        self.assertEqual(self.reader.read_collision(self.gateway), "INIT")
        self.assertEqual(timeline, ["/robot/arms/right/joints",
                         ORIGIN + "/rw/motionsystem/mechunits/ROB_R/jointtarget?json=1",
                         "/robot/arms/left/joints",
                         ORIGIN + "/rw/motionsystem/mechunits/ROB_L/jointtarget?json=1",
                         ORIGIN + "/rw/panel/coldetstate?json=1"])
        for call in self.native.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertIs(call.kwargs["allow_redirects"], False)
            self.assertIs(call.kwargs["stream"], True)
            self.assertIsInstance(call.kwargs["auth"], HTTPDigestAuth)
            self.assertEqual(set(call.kwargs), {"timeout", "allow_redirects", "auth", "stream"})
        for response in self.responses:
            response.close.assert_called_once()
        self.post.assert_not_called()

    def test_repeated_reads_revalidate_both_arms_without_cache(self):
        self.queue()
        self.assertEqual(self.reader.read_collision(self.gateway), "INIT")
        self.queue(left={**native_arm("left"), "eax_a": "136"})
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.reader.read_collision(self.gateway)
        self.assertEqual(self.gateway.call_count, 4)
        self.assertEqual(self.native.call_count, 5)

    def test_exact_states_preserved_including_triggered(self):
        for state in ("INIT", "CONFIRMED", "TRIGGERED", "TRIGGERED_ACK"):
            with self.subTest(state=state):
                self.queue(collision=state)
                self.assertEqual(self.reader.read_collision(self.gateway), state)

    def test_all_seven_joints_on_both_arms_required_with_inclusive_tolerance(self):
        for arm in ("right", "left"):
            for field in [f"rax_{i}" for i in range(1, 7)] + ["eax_a"]:
                with self.subTest(arm=arm, field=field):
                    changed = native_arm(arm)
                    changed[field] = str(float(changed[field]) + .1)
                    self.queue(**{arm: changed})
                    self.assertEqual(self.reader.read_collision(self.gateway), "INIT")
                    changed[field] = str(float(changed[field]) + .0001)
                    self.queue(**{arm: changed})
                    before = self.native.call_count
                    with self.assertRaisesRegex(ValueError, "do not match"):
                        self.reader.read_collision(self.gateway)
                    self.assertEqual(self.native.call_count - before, 1 if arm == "right" else 2)

    def test_malformed_gateway_status_stops_before_native_or_collision_read(self):
        base = gateway_arm("right")
        values = [None, [], {}, {**base, "arm": "left"}, {**base, "mech_unit": "ROB_L"},
                  {**base, "joints": [0] * 7}, {**base, "joints": [0] * 5},
                  {**base, "joints": "0,-130,30,0,40,0"},
                  {**base, "arm_angle": "-135"}, {**base, "arm_angle": None}]
        values += [{**base, "joints": [bad] + base["joints"][1:]}
                   for bad in (True, None, "0", float("nan"), float("inf"), 10**1000)]
        for value in values:
            with self.subTest(value=value):
                self.gateway.side_effect = None
                self.gateway.return_value = value
                with self.assertRaises(ValueError):
                    self.reader.read_collision(self.gateway)
        self.native.assert_not_called()

    def test_optional_mechanical_unit_can_be_absent_but_arm_identity_cannot(self):
        self.queue()
        self.gateway.side_effect = lambda endpoint: {
            k: v for k, v in gateway_arm(endpoint.split("/")[-2]).items() if k != "mech_unit"}
        self.assertEqual(self.reader.read_collision(self.gateway), "INIT")

    def test_invalid_native_axis_cannot_confirm_clearance(self):
        for bad in (None, True, "nan", "inf", float("nan"), float("inf"), 10**1000, "", "not a joint"):
            with self.subTest(bad=bad):
                self.queue(right={**native_arm("right"), "rax_3": bad})
                before = self.native.call_count
                with self.assertRaises(ValueError):
                    self.reader.read_collision(self.gateway)
                self.assertEqual(self.native.call_count - before, 1)
                self.responses[0].close.assert_called_once()

    def test_missing_or_duplicate_normalized_native_fields_rejected(self):
        for entry in ({k: v for k, v in native_arm("right").items() if k != "eax_a"},
                      {**native_arm("right"), "rax_1 ": "123"}):
            with self.subTest(entry=entry):
                self.queue(right=entry)
                with self.assertRaises(ValueError):
                    self.reader.read_collision(self.gateway)

    def test_malformed_native_envelope_redirect_and_large_reply_fail_closed(self):
        for payload in ([], {"_embedded": []}, {"_embedded": {}},
                        {"_embedded": {"_state": []}}, {"_embedded": {"_state": [1]}},
                        {"_embedded": {"_state": [native_arm("right"), native_arm("right")]}}):
            with self.subTest(payload=payload):
                response = reply(payload=payload)
                self.native.side_effect = [response]
                with self.assertRaises(ValueError):
                    self.reader.read_collision(self.gateway)
                response.close.assert_called_once()
        for status, content in ((302, b""), (200, b"x" * (MAX_RESPONSE_BYTES + 1)), (401, b"")):
            response = reply(native_arm("right"), status=status)
            response.content = content
            self.native.side_effect = [response]
            with self.assertRaises(ValueError):
                self.reader.read_collision(self.gateway)
            response.close.assert_called_once()

    def test_unknown_or_ambiguous_collision_state_is_never_clear(self):
        for value in (None, 0, True, "init", " INIT", "INIT ", "UNKNOWN"):
            with self.subTest(value=value):
                self.queue(collision=value)
                with self.assertRaisesRegex(ValueError, "unknown collision"):
                    self.reader.read_collision(self.gateway)
        self.queue()
        self.responses[-1] = reply({"coldetstate": "INIT", "coldetstate ": "TRIGGERED"})
        self.native.side_effect = self.responses
        with self.assertRaises(ValueError):
            self.reader.read_collision(self.gateway)

    def test_conflicting_exact_json_field_cannot_be_hidden_by_decoder(self):
        self.queue()
        self.responses[-1].content = (b'{"_embedded":{"_state":[{"coldetstate":"TRIGGERED",'
                                      b'"coldetstate":"INIT"}]}}')
        with self.assertRaises(ValueError):
            self.reader.read_collision(self.gateway)
        self.responses[-1].close.assert_called_once()

    def test_stream_limit_stops_reading_and_closes_response(self):
        self.queue()
        consumed = []
        def chunks(**kwargs):
            for index in range(20):
                consumed.append(index)
                yield b"x" * 4096
        self.responses[0].iter_content.side_effect = chunks
        with self.assertRaises(ValueError):
            self.reader.read_collision(self.gateway)
        self.assertEqual(len(consumed), MAX_RESPONSE_BYTES // 4096 + 1)
        self.responses[0].close.assert_called_once()

    def test_expired_response_is_rejected_before_json_acceptance(self):
        self.queue()
        with patch("controller_status.time.monotonic", side_effect=[100.0, 100.1, 105.1]):
            with self.assertRaises(ValueError):
                self.reader.read_collision(self.gateway)
        self.responses[0].close.assert_called_once()

    def test_transport_and_gateway_errors_are_scrubbed(self):
        for error in (requests.Timeout("private password"), requests.ConnectionError("private password")):
            self.native.side_effect = error
            with self.assertRaises(ValueError) as result:
                self.reader.read_collision(self.gateway)
            self.assertNotIn("private", str(result.exception))
        self.gateway.side_effect = RuntimeError("private token")
        with self.assertRaises(ValueError) as result:
            self.reader.read_collision(self.gateway)
        self.assertNotIn("private", str(result.exception))
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
