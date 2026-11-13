"""Offline gateway contract fixtures; no device, external fixtures or HTTP needed."""

import copy
import json
import unittest

from gateway_capabilities import ACTIONS, parse_gateway_capabilities


def schema_fixture(*, native=False, collisions=False, prefix="/api/v1"):
    """Minimal faithful shapes from Lynx 0.4.0 and the guarded reset extension."""
    schemas = {
        "Status": {"type": "object", "properties": {
            key: {"type": "string"} for key in ("execution_state", "controller_state", "operation_mode")}},
        "Command": {"type": "object", "properties": {
            "accepted": {"type": "boolean"}, "message": {"type": "string"}}},
        "Motion": {"type": "object", "properties": {
            "arm": {"type": "string", "enum": ["left", "right"]},
            "wait": {"type": "boolean", "default": True}}},
        "Joints": {"type": "object", "properties": {
            "arm": {"type": "string"}, "joints": {"type": "array", "items": {"type": "number"}},
            "arm_angle": {"type": "number"}}},
        "LegacyStart": {"type": "object", "properties": {
            "reset_program_pointer": {"type": "boolean", "default": False}}},
        "Collision": {"type": "object", "properties": {
            "collision_state": {"type": "string", "enum": ["INIT", "TRIGGERED", "CONFIRMED", "TRIGGERED_ACK"]}}},
    }
    schemas["Gripper"] = copy.deepcopy(schemas["Motion"])
    schemas["Gripper"]["properties"]["action"] = {"type": "string", "enum": ["open", "grip", "move"]}
    paths = {}

    def add(method, endpoint, response=None, request=None):
        operation = {"responses": {"200": {"description": "Successful Response"}}}
        if response:
            operation["responses"]["200"]["content"] = {"application/json": {"schema": {
                "$ref": "#/components/schemas/" + response}}}
        if request:
            value = {"$ref": "#/components/schemas/" + request}
            if endpoint.endswith("/home"):
                value = {"anyOf": [value, {"type": "null"}]}
            operation["requestBody"] = {"content": {"application/json": {"schema": value}}}
        paths.setdefault(prefix + endpoint, {})[method] = operation

    for endpoint in ("acquire", "renew", "release"):
        add("post", "/control/" + endpoint)
    add("get", "/robot/rapid", "Status")
    add("post", "/robot/rapid/stop", "Status")
    add("post", "/robot/rapid/start", "Status", "LegacyStart")
    add("post", "/robot/rws/home", "Command", "Motion")
    add("post", "/robot/rws/gripper", "Command", "Gripper")
    add("get", "/robot/arms/{arm}/joints", "Joints")
    if native:
        schemas["Status"]["properties"]["stop_generation"] = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        schemas["Reset"] = {"type": "object", "properties": {
            "start": {"type": "boolean", "default": False},
            "stop_generation": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}}}
        add("post", "/robot/rapid/reset", "Status", "Reset")
    if collisions:
        add("get", "/robot/rapid/collision", "Collision")
        add("post", "/robot/rapid/collision/ack", "Collision")
    return {"openapi": "3.1.0", "info": {"title": "Robot & Camera Gateway", "version": "0.4.0"},
            "paths": paths, "components": {"schemas": schemas}}


class GatewayCapabilitiesTests(unittest.TestCase):
    def test_lynx_contract_advertises_legacy_start_without_enabling_it(self):
        result = parse_gateway_capabilities(schema_fixture())
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["gateway_version"], "0.4.0")
        self.assertEqual(result["supported"], {name: name in {"home", "open", "grip", "robot_status", "stop"}
                                             for name in ACTIONS})
        self.assertTrue(result["protocols"]["legacy_start_reset_pp"])
        self.assertFalse(result["protocols"]["native_reset_generation"])
        for name in ("rapid_reset", "rapid_start"):
            self.assertIsNone(result["actions"][name]["endpoint"])
            self.assertIsNone(result["actions"][name]["protocol"])
            self.assertIn("cannot safely cancel a delayed restart", result["actions"][name]["reason"])
        self.assertEqual(result["protocol_details"]["legacy_start_reset_pp"]["request_body"],
                         {"reset_program_pointer": True})
        self.assertFalse(result["protocol_details"]["legacy_start_reset_pp"]["usable_for_guarded_restart"])

    def test_native_guarded_gateway_enables_all_nine_actions(self):
        result = parse_gateway_capabilities(schema_fixture(native=True, collisions=True))
        self.assertEqual(set(result["supported"]), set(ACTIONS))
        self.assertTrue(all(result["supported"].values()))
        self.assertEqual(result["actions"]["rapid_start"]["endpoint"], "/robot/rapid/reset")
        self.assertEqual(result["actions"]["rapid_start"]["protocol"], "native_reset_generation")
        self.assertEqual(result["actions"]["home"]["protocol"], "rws_wait_and_joint_readback")

    def test_generation_must_be_advertised_by_status_and_reset_body(self):
        for container, name in (("Status", "stop_generation"), ("Reset", "stop_generation"), ("Reset", "start")):
            schema = schema_fixture(native=True)
            del schema["components"]["schemas"][container]["properties"][name]
            result = parse_gateway_capabilities(schema)
            self.assertFalse(result["protocols"]["native_reset_generation"])
            self.assertFalse(result["supported"]["rapid_start"])
            self.assertFalse(result["supported"]["rapid_reset"])

    def test_string_generation_and_false_only_start_are_not_native_protocol(self):
        for value in ({"type": "string"}, {"type": "null"}):
            schema = schema_fixture(native=True)
            schema["components"]["schemas"]["Status"]["properties"]["stop_generation"] = value
            self.assertFalse(parse_gateway_capabilities(schema)["supported"]["rapid_start"])
        schema = schema_fixture(native=True)
        schema["components"]["schemas"]["Reset"]["properties"]["start"] = {"type": "boolean", "enum": [False]}
        self.assertFalse(parse_gateway_capabilities(schema)["protocols"]["native_reset_generation"])

    def test_reset_response_contract_is_required(self):
        schema = schema_fixture(native=True)
        schema["paths"]["/api/v1/robot/rapid/reset"]["post"]["responses"] = {}
        self.assertFalse(parse_gateway_capabilities(schema)["supported"]["rapid_start"])

    def test_missing_joint_readback_disables_home_only(self):
        schema = schema_fixture()
        del schema["paths"]["/api/v1/robot/arms/{arm}/joints"]
        result = parse_gateway_capabilities(schema)
        self.assertFalse(result["supported"]["home"])
        self.assertTrue(result["supported"]["open"])
        self.assertTrue(result["supported"]["grip"])
        self.assertIn({"method": "GET", "path": "/api/v1/robot/arms/{arm}/joints"},
                      result["actions"]["home"]["missing"])

    def test_missing_lease_disables_mutations_but_not_readonly_status(self):
        schema = schema_fixture(native=True, collisions=True)
        del schema["paths"]["/api/v1/control/renew"]
        result = parse_gateway_capabilities(schema)
        self.assertEqual({name for name, supported in result["supported"].items() if supported},
                         {"robot_status", "collision_status"})
        self.assertTrue(result["protocols"]["native_reset_generation"])

    def test_collision_readback_is_required_for_ack_but_optional_for_home(self):
        schema = schema_fixture(collisions=True)
        del schema["paths"]["/api/v1/robot/rapid/collision"]
        result = parse_gateway_capabilities(schema)
        self.assertFalse(result["supported"]["collision_ack"])
        self.assertFalse(result["supported"]["collision_status"])
        self.assertTrue(result["supported"]["home"])

    def test_gripper_action_enum_discriminates_open_from_grip(self):
        schema = schema_fixture()
        schema["components"]["schemas"]["Gripper"]["properties"]["action"]["enum"] = ["open"]
        result = parse_gateway_capabilities(schema)
        self.assertTrue(result["supported"]["open"])
        self.assertFalse(result["supported"]["grip"])

    def test_wait_true_and_both_individual_arms_are_required(self):
        for name, value in (("wait", {"type": "boolean", "const": False}),
                            ("arm", {"type": "string", "enum": ["left"]})):
            schema = schema_fixture()
            schema["components"]["schemas"]["Motion"]["properties"][name] = value
            self.assertFalse(parse_gateway_capabilities(schema)["supported"]["home"])

    def test_wrong_method_does_not_count_as_available(self):
        schema = schema_fixture()
        route = schema["paths"]["/api/v1/robot/rws/home"]
        route["get"] = route.pop("post")
        result = parse_gateway_capabilities(schema)
        self.assertFalse(result["supported"]["home"])
        self.assertFalse(result["actions"]["home"]["advertised"])

    def test_custom_prefix_and_root_prefix_are_explicit(self):
        for prefix in ("/custom/api", ""):
            result = parse_gateway_capabilities(schema_fixture(prefix=prefix), api_prefix=prefix + "/")
            self.assertTrue(result["supported"]["home"])
            self.assertEqual(result["actions"]["home"]["endpoint"], "/robot/rws/home")
            self.assertEqual(result["actions"]["home"]["requires"][0]["path"], prefix + "/robot/rws/home")
        self.assertFalse(parse_gateway_capabilities(schema_fixture(prefix=""))["supported"]["home"])

    def test_invalid_prefix_is_rejected_without_normalizing_to_another_host(self):
        for prefix in (None, "api/v1", "https://host/api", "/api?x=1", "/a/../b", "/api\\v1"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                parse_gateway_capabilities(schema_fixture(), prefix)

    def test_malformed_documents_fail_closed(self):
        for value in (None, [], "schema", {}, {"openapi": "3.1.0", "paths": []},
                      {"openapi": "2.0", "paths": {}}, {"openapi": True, "paths": {}}):
            result = parse_gateway_capabilities(value)
            self.assertFalse(result["schema_valid"])
            self.assertFalse(any(result["supported"].values()))
            self.assertIsNotNone(result["error"])

    def test_broken_external_and_cyclic_refs_never_enable_native_reset(self):
        for reference in ("#/components/schemas/Absent", "https://remote.invalid/schema", "#/components/schemas/Reset"):
            schema = schema_fixture(native=True)
            schema["components"]["schemas"]["Reset"] = {"$ref": reference}
            result = parse_gateway_capabilities(schema)
            self.assertFalse(result["supported"]["rapid_start"])
            self.assertTrue(result["supported"]["robot_status"])

    def test_inherited_and_inline_fields_are_supported(self):
        schema = schema_fixture(native=True)
        reset = schema["components"]["schemas"]["Reset"]
        schema["components"]["schemas"]["Reset"] = {"allOf": [reset]}
        response = schema["paths"]["/api/v1/robot/rapid"]["get"]["responses"]["200"]
        response["content"]["application/json"]["schema"] = schema["components"]["schemas"]["Status"]
        self.assertTrue(parse_gateway_capabilities(schema)["supported"]["rapid_start"])

    def test_conflicting_allof_and_boolean_numeric_enum_fail_closed(self):
        for value in ({"allOf": [{"type": "boolean"}, {"enum": [False]}]},
                      {"allOf": [{"type": "boolean"}, {"type": "string"}]},
                      {"type": "boolean", "enum": [0, 1]}):
            schema = schema_fixture(native=True)
            schema["components"]["schemas"]["Reset"]["properties"]["start"] = value
            self.assertFalse(parse_gateway_capabilities(schema)["protocols"]["native_reset_generation"])

    def test_boolean_oneof_can_advertise_both_concrete_values(self):
        schema = schema_fixture(native=True)
        schema["components"]["schemas"]["Reset"]["properties"]["start"] = {
            "oneOf": [{"type": "boolean", "const": True}, {"type": "boolean", "const": False}]}
        self.assertTrue(parse_gateway_capabilities(schema)["supported"]["rapid_start"])

    def test_session_header_advertisement_accepts_referenced_parameters(self):
        schema = schema_fixture()
        schema["components"]["parameters"] = {"Session": {"name": "X-Control-Session", "in": "header", "schema": {"type": "string"}}}
        schema["paths"]["/api/v1/control/renew"]["post"]["parameters"] = [{"$ref": "#/components/parameters/Session"}]
        self.assertTrue(parse_gateway_capabilities(schema)["protocols"]["control_session_header"])

    def test_repeated_reference_fanout_is_bounded_and_fails_closed(self):
        schema = schema_fixture(native=True)
        models = schema["components"]["schemas"]
        models["Layer0"] = {"type": "boolean"}
        for number in range(1, 12):
            models["Layer" + str(number)] = {"allOf": [
                {"$ref": "#/components/schemas/Layer" + str(number - 1)} for _ in range(8)]}
        models["Reset"]["properties"]["start"] = {"$ref": "#/components/schemas/Layer11"}
        self.assertFalse(parse_gateway_capabilities(schema)["supported"]["rapid_start"])

    def test_input_is_unchanged_and_returned_structures_are_independent(self):
        schema = schema_fixture(native=True, collisions=True)
        before = copy.deepcopy(schema)
        result = parse_gateway_capabilities(schema)
        self.assertEqual(schema, before)
        json.dumps(result, allow_nan=False)
        result["actions"]["home"]["requires"].clear()
        self.assertTrue(parse_gateway_capabilities(schema)["actions"]["home"]["requires"])


if __name__ == "__main__":
    unittest.main()
