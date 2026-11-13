"""Pure discovery of the gateway contracts used by explicit robot controls.

This module performs no HTTP, reads no files, and authorizes no movement.
An advertised route is not evidence that a controller is ready or permission
has been granted. Runtime lease, status, inspection and completion checks remain
mandatory. A legacy start route never substitutes for generation-fenced reset.
"""

import re


ACTIONS = ("home", "open", "grip", "robot_status", "stop", "rapid_reset",
           "rapid_start", "collision_ack", "collision_status")
_LEASE = (("POST", "/control/acquire"), ("POST", "/control/renew"),
          ("POST", "/control/release"))
_STATUS = ("GET", "/robot/rapid")
_STOP = ("POST", "/robot/rapid/stop")
_COLLISION = ("GET", "/robot/rapid/collision")
_JOINTS = ("GET", "/robot/arms/{arm}/joints")
_GUARDED_REASON = ("This gateway cannot safely cancel a delayed restart; "
                   "guarded reset support is required.")
_UNSPECIFIED = object()


class _Schema:
    """Bounded local-reference and schema-composition lookup; never fetch refs."""

    def __init__(self, document, prefix):
        self.document = document
        self.prefix = prefix
        self.paths = document.get("paths", {})

    def resolve(self, value, seen=(), depth=0):
        if not isinstance(value, dict) or depth > 16:
            return None
        reference = value.get("$ref")
        if reference is None:
            return value
        if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen:
            return None
        target = self.document
        for part in reference[2:].split("/"):
            if not isinstance(target, dict):
                return None
            target = target.get(part.replace("~1", "/").replace("~0", "~"))
        resolved = self.resolve(target, seen + (reference,), depth + 1)
        if resolved is None:
            return None
        # FastAPI places a default beside a ref. Preserve siblings without
        # allowing them to manufacture properties for an unresolved reference.
        return {**resolved, **{key: item for key, item in value.items() if key != "$ref"}}

    def nodes(self, value, depth=0, visited=()):
        if depth > 16 or not isinstance(value, dict) or id(value) in visited:
            return None
        current = self.resolve(value)
        if current is None:
            return None
        result = [current]
        for key in ("allOf", "anyOf", "oneOf"):
            if key not in current:
                continue
            children = current[key]
            if not isinstance(children, list) or not 1 <= len(children) <= 16:
                return None
            for child in children:
                nested = self.nodes(child, depth + 1, visited + (id(value),))
                if nested is None or len(result) + len(nested) > 128:
                    return None
                result.extend(nested)
        return result

    def operation(self, method, endpoint):
        path = self.resolve(self.paths.get(self.prefix + endpoint))
        return self.resolve(path.get(method.lower())) if path is not None else None

    def body(self, method, endpoint):
        operation = self.operation(method, endpoint)
        if operation is None:
            return None
        request = self.resolve(operation.get("requestBody"))
        return self.content(request)

    def response(self, method, endpoint):
        operation = self.operation(method, endpoint)
        responses = operation.get("responses") if operation is not None else None
        if not isinstance(responses, dict):
            return None
        # These clients consume a JSON 200 reply, not an asynchronous 202 job.
        return self.content(self.resolve(responses.get("200")))

    @staticmethod
    def content(value):
        content = value.get("content") if isinstance(value, dict) else None
        media = content.get("application/json") if isinstance(content, dict) else None
        return media.get("schema") if isinstance(media, dict) else None

    def property(self, schema, name):
        nodes = self.nodes(schema)
        if nodes is None:
            return None
        matches = [node["properties"][name] for node in nodes
                   if isinstance(node.get("properties"), dict) and name in node["properties"]]
        if not matches:
            return None
        return matches[0] if len(matches) == 1 else {"allOf": matches}

    def has_type(self, value, kind, *, allowed=()):
        # Check every requested concrete value separately: a boolean oneOf
        # may legitimately separate true and false. allOf constraints must all
        # permit the same value; collecting types alone would ignore conflicts.
        for item in (allowed or (_UNSPECIFIED,)):
            budget = [256]
            compatible, typed = self._type_contract(value, kind, item, budget=budget)
            if budget[0] < 0 or not compatible or not typed:
                return False
        return True

    def _type_contract(self, value, kind, wanted, depth=0, visited=(), budget=None):
        """Return (compatible, explicitly typed), with bounded composition."""
        budget[0] -= 1
        if budget[0] < 0 or depth > 16 or not isinstance(value, dict) or id(value) in visited:
            return False, False
        current = self.resolve(value)
        if current is None:
            return False, False
        declared = current.get("type")
        typed = declared == kind or isinstance(declared, list) and kind in declared
        if declared is not None and not typed:
            return False, False

        def same(left, right):
            return type(left) is type(right) and left == right

        def kind_matches(item):
            return {"boolean": type(item) is bool, "integer": type(item) is int,
                    "number": type(item) in (int, float), "string": isinstance(item, str),
                    "array": isinstance(item, list)}.get(kind, False)

        if "enum" in current:
            values = current["enum"]
            if not isinstance(values, list) or not values:
                return False, False
            if not (any(kind_matches(item) for item in values) if wanted is _UNSPECIFIED else
                    any(same(item, wanted) for item in values)):
                return False, False
        if "const" in current:
            if not (kind_matches(current["const"]) if wanted is _UNSPECIFIED else same(current["const"], wanted)):
                return False, False
        for key in ("allOf", "anyOf", "oneOf"):
            if key not in current:
                continue
            children = current[key]
            if not isinstance(children, list) or not 1 <= len(children) <= 16:
                return False, False
            matches = [self._type_contract(child, kind, wanted, depth + 1, visited + (id(value),), budget)
                       for child in children]
            compatible = [item for item in matches if item[0]]
            if ((key == "allOf" and len(compatible) != len(children))
                    or key in ("anyOf", "oneOf") and not compatible
                    or key == "oneOf" and wanted is not _UNSPECIFIED and len(compatible) != 1):
                return False, False
            typed = typed or any(item[1] for item in compatible)
        return True, bool(typed)

    def fields(self, schema, expected):
        return all(self.has_type(self.property(schema, key), kind, allowed=allowed)
                   for key, kind, allowed in expected)


def parse_gateway_capabilities(schema, api_prefix="/api/v1"):
    """Return supported action booleans and dependency/protocol metadata.

    ``actions[*].endpoint`` is relative to RobotClient.api. Dependency ``path``
    entries include api_prefix. Unsupported legacy start is advertised only in
    protocol_details; it cannot produce an executable rapid_start action.
    Missing/malformed schema data disables capabilities. An invalid API prefix
    is a caller configuration error and raises ValueError.
    """
    if (not isinstance(api_prefix, str) or len(api_prefix) > 256
            or not re.fullmatch(r"(?:/[A-Za-z0-9_~.-]+)*/?", api_prefix)
            or any(part in (".", "..") for part in api_prefix.split("/"))):
        raise ValueError("API prefix must be an absolute URL path without a query or traversal.")
    prefix = api_prefix.rstrip("/")
    valid = (isinstance(schema, dict) and isinstance(schema.get("openapi"), str)
             and schema["openapi"].startswith("3.") and isinstance(schema.get("paths"), dict)
             and len(schema["paths"]) <= 4096)
    document = schema if valid else {"paths": {}}
    reader = _Schema(document, prefix)
    info = document.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or len(version) > 80:
        version = None
    status_schema = reader.response(*_STATUS)
    status_contract = reader.fields(status_schema, [
        (key, "string", ()) for key in ("execution_state", "controller_state", "operation_mode")])
    joint_readback = bool(reader.operation(*_JOINTS) is not None and
                          reader.fields(reader.response(*_JOINTS),
                                        [("arm", "string", ()), ("joints", "array", ()),
                                         ("arm_angle", "number", ())]))
    generation = reader.has_type(reader.property(status_schema, "stop_generation"), "integer")
    reset_body = reader.body("POST", "/robot/rapid/reset")
    reset_contract = reader.fields(reset_body, [("start", "boolean", (False, True)),
                                               ("stop_generation", "integer", ())])
    native = bool(reader.operation("POST", "/robot/rapid/reset") is not None
                  and generation and reset_contract and status_contract and
                  reader.fields(reader.response("POST", "/robot/rapid/reset"), [
                      (key, "string", ()) for key in ("execution_state", "controller_state", "operation_mode")]))
    legacy = bool(reader.operation("POST", "/robot/rapid/start") is not None and
                  reader.fields(reader.body("POST", "/robot/rapid/start"),
                                [("reset_program_pointer", "boolean", (True,))]))
    session_header = False
    for path in reader.paths.values():
        path = reader.resolve(path)
        if path is None:
            continue
        for operation in [path, *(path.get(key) for key in ("get", "post", "put", "delete"))]:
            operation = reader.resolve(operation)
            parameters = operation.get("parameters", []) if operation is not None else []
            if not isinstance(parameters, list):
                continue
            for parameter in parameters[:128]:
                parameter = reader.resolve(parameter)
                if (parameter is not None and parameter.get("in") == "header"
                        and str(parameter.get("name", "")).lower() == "x-control-session"):
                    session_header = True

    result = {"schema_valid": bool(valid), "gateway_version": version, "api_prefix": prefix,
              "supported": {}, "actions": {},
              "protocols": {"native_reset_generation": native,
                            "joint_readback": joint_readback,
                            "legacy_start_reset_pp": legacy,
                            "control_session_header": session_header},
              "protocol_details": {
                  "legacy_start_reset_pp": {"advertised": legacy,
                      "method": "POST", "path": prefix + "/robot/rapid/start",
                      "request_body": {"reset_program_pointer": True},
                      "usable_for_guarded_restart": False},
                  "native_reset_generation": {"advertised": native,
                      "method": "POST", "path": prefix + "/robot/rapid/reset",
                      "generation_source": prefix + "/robot/rapid"}},
              "error": None if valid else "Gateway did not provide a valid OpenAPI 3 schema."}

    def add(name, endpoint, method, requires, contract=True, protocol=None, reason=None):
        dependencies = list(dict.fromkeys([(method, endpoint), *requires]))
        missing = [{"method": verb, "path": prefix + path} for verb, path in dependencies
                   if reader.operation(verb, path) is None]
        supported = bool(valid and not missing and contract)
        explanation = None if supported else (result["error"] or reason or
            ("Gateway is missing required routes: " + ", ".join(item["method"] + " " + item["path"]
                                                               for item in missing) if missing else
             "Gateway does not advertise the response or request fields required by this control."))
        result["supported"][name] = supported
        result["actions"][name] = {"supported": supported,
            "advertised": reader.operation(method, endpoint) is not None,
            "endpoint": endpoint if supported else None, "method": method if supported else None,
            "protocol": protocol if supported else None,
            "requires": [{"method": verb, "path": prefix + path} for verb, path in dependencies],
            "missing": missing, "reason": explanation}

    motion_dependencies = (*_LEASE, _STATUS, _STOP)
    motion_body = [("arm", "string", ("left", "right")), ("wait", "boolean", (True,))]
    command_reply = [("accepted", "boolean", ()), ("message", "string", ())]
    home_contract = (status_contract and reader.fields(reader.body("POST", "/robot/rws/home"), motion_body)
                     and reader.fields(reader.response("POST", "/robot/rws/home"), command_reply)
                     and joint_readback)
    add("home", "/robot/rws/home", "POST", (*motion_dependencies, ("GET", "/robot/arms/{arm}/joints")),
        home_contract, "rws_wait_and_joint_readback")
    for action in ("open", "grip"):
        contract = (status_contract and reader.fields(reader.body("POST", "/robot/rws/gripper"),
                    [*motion_body, ("action", "string", (action,))])
                    and reader.fields(reader.response("POST", "/robot/rws/gripper"), command_reply))
        add(action, "/robot/rws/gripper", "POST", motion_dependencies, contract, "rws_wait")
    add("robot_status", "/robot/rapid", "GET", (), status_contract, "rapid_status")
    add("stop", "/robot/rapid/stop", "POST", _LEASE,
        reader.fields(reader.response(*_STOP), [("execution_state", "string", ())]), "rapid_stop")
    for action in ("rapid_reset", "rapid_start"):
        add(action, "/robot/rapid/reset", "POST", motion_dependencies, native,
            "native_reset_generation", reason=_GUARDED_REASON if not native else None)
    collision_contract = reader.fields(reader.response(*_COLLISION), [("collision_state", "string", ())])
    add("collision_status", "/robot/rapid/collision", "GET", (), collision_contract, "collision_status")
    ack_contract = (status_contract and collision_contract and
                    reader.fields(reader.response("POST", "/robot/rapid/collision/ack"),
                                  [("collision_state", "string", ())]))
    add("collision_ack", "/robot/rapid/collision/ack", "POST", (*motion_dependencies, _COLLISION),
        ack_contract, "collision_ack_with_readback")
    return result
