"""Bounded path dispatch, complete-job barriers and nested callback isolation."""

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from robot_client import RobotClient, RobotCommandError
from gateway_capabilities import parse_gateway_capabilities
from test_gateway_capabilities import schema_fixture


def path_job(**overrides):
    body = {
        "arm": "left", "wait": True, "speed": 100, "acceleration": 50,
        "ramp": 50, "zone": 10, "max_acceleration": 1.5,
        "points": [{"x": 450, "y": 100, "z": 350, "rx": 180, "ry": 0, "rz": 180,
                    "linear": True, "speed": 100, "zone": 5},
                   {"x": 460, "y": 100, "z": 350, "rx": 180, "ry": 0, "rz": 180,
                    "linear": False, "speed": 80, "zone": 0}],
    }
    body.update(overrides)
    return {"name": "left_path", "endpoint": RobotClient.PATH_ENDPOINT, "body": body}


@pytest.fixture
def gateway(monkeypatch):
    state = {"replies": [], "on_path": None, "schema": schema_fixture(blended_path=True)}

    def post(url, **options):
        if url.endswith(("/control/acquire", "/control/renew")):
            payload = {"success": True, "lease": {"active": True, "session_id": "path-test",
                       "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()}}
        elif url.endswith("/control/release"):
            payload = {"success": True, "lease": {"active": False}}
        elif url.endswith("/robot/rws/path"):
            if state["on_path"] is not None:
                state["on_path"]()
            payload = state["replies"].pop(0) if state["replies"] else {"accepted": True, "message": "DONE"}
        else:
            raise AssertionError(f"Unexpected endpoint: {url}")
        if isinstance(payload, Exception):
            raise payload
        response = Mock(status_code=200, headers={})
        response.json.return_value = payload
        return response

    state["post"] = Mock(side_effect=post)
    monkeypatch.setattr("robot_client.requests.post", state["post"])
    def get(url, **options):
        assert url == "http://offline.invalid/openapi.json"
        assert options["allow_redirects"] is False
        response = Mock(status_code=200, content=b"fixture")
        response.json.return_value = state["schema"]
        return response

    state["get"] = Mock(side_effect=get)
    monkeypatch.setattr("robot_client.requests.get", state["get"])
    monkeypatch.setattr("socket.socket.connect", Mock(side_effect=AssertionError("Network forbidden")))
    state["client"] = RobotClient("http://offline.invalid/api/v1", {}, require_session=True)
    return state


def path_calls(gateway):
    return [call for call in gateway["post"].call_args_list if call.args[0].endswith(RobotClient.PATH_ENDPOINT)]


def malformed_bodies():
    yield None
    for key, value in (("arm", "both"), ("wait", False), ("wait", 1), ("command", "MoveJ arbitrary"),
                       ("points", []), ("points", [path_job()["body"]["points"][0]] * 9),
                       ("points", (path_job()["body"]["points"][0],)), ("points", ["GOTO"]),
                       ("points", [{}])):
        yield {**path_job()["body"], key: value}
    for field in ("arm", "wait", "points"):
        body = path_job()["body"]
        del body[field]
        yield body
    limits = {"speed": (1, 1500), "acceleration": (20, 100), "ramp": (10, 100),
              "zone": (0, 100), "max_acceleration": (.1, 20)}
    for field, (low, high) in limits.items():
        for value in (True, None, "10", float("nan"), float("inf"), 10 ** 500, low - .01, high + .01):
            yield {**path_job()["body"], field: value}
    for field in ("x", "y", "z", "rx", "ry", "rz"):
        for value in (None, False, "0", float("nan"), float("inf"), 10 ** 500):
            body = path_job()["body"]
            body["points"][1][field] = value
            yield body
        body = path_job()["body"]
        del body["points"][1][field]
        yield body
    for field, values in (("linear", (0, 1, None, "false")), ("extra", ("MoveL",)),
                          ("speed", (True, None, 0, 1501, float("nan"))),
                          ("zone", (True, None, -1, 101, float("inf")))):
        for value in values:
            body = path_job()["body"]
            body["points"][1][field] = value
            yield body


@pytest.mark.parametrize("body", list(malformed_bodies()))
def test_invalid_later_path_prevents_the_entire_plan_before_http(gateway, body):
    bad = {**path_job(), "body": body}
    with pytest.raises(RobotCommandError):
        gateway["client"].execute_jobs([path_job(), bad])
    gateway["post"].assert_not_called()
    gateway["get"].assert_not_called()


@pytest.mark.parametrize("count", (1, 8))
def test_path_point_count_boundaries_and_omitted_options(gateway, count):
    point = {key: value for key, value in path_job()["body"]["points"][0].items()
             if key in {"x", "y", "z", "rx", "ry", "rz"}}
    job = path_job()
    job["body"] = {"arm": "right", "wait": True, "points": [dict(point) for _ in range(count)]}
    completed = Mock()
    gateway["client"].execute_jobs([job], after_done=completed)
    assert len(path_calls(gateway)) == 1
    assert path_calls(gateway)[0].kwargs["json"] == job["body"]
    assert path_calls(gateway)[0].kwargs["headers"]["X-Control-Session"] == "path-test"
    completed.assert_called_once_with(0, job)


def test_dry_run_validates_without_network_or_callbacks(gateway, capsys):
    before, after = Mock(), Mock()
    RobotClient("http://offline.invalid", {}, dry_run=True).execute_jobs(
        [path_job()], before_send=before, after_done=after)
    gateway["post"].assert_not_called()
    gateway["get"].assert_not_called()
    before.assert_not_called()
    after.assert_not_called()
    assert "DRY RUN: POST /robot/rws/path" in capsys.readouterr().out


@pytest.mark.parametrize("reply", ({"accepted": True, "message": "RUNNING"},
                                  {"accepted": False, "message": "DONE"},
                                  {"accepted": 1, "message": "DONE"},
                                  {"accepted": True, "message": "ERR path failed"},
                                  requests.Timeout("path timed out")))
def test_unconfirmed_path_stops_plan_without_retry_or_point_fallback(gateway, reply):
    gateway["replies"] = [reply]
    after = Mock()
    with pytest.raises(RobotCommandError):
        gateway["client"].execute_jobs([path_job(), path_job()], after_done=after)
    assert len(path_calls(gateway)) == 1
    assert gateway["post"].call_args_list[-1].args[0].endswith("/control/release")
    after.assert_not_called()


def test_initial_plan_and_callback_point_mutations_cannot_change_dispatched_points(gateway):
    jobs = [path_job(), path_job()]
    expected = copy.deepcopy(jobs)
    before_copies = []
    completed = []

    def before(index, job):
        before_copies.append(job)
        job["body"]["points"][0]["x"] = float("nan")
        job["body"]["points"].append({"command": "MOVE"})
        jobs[1]["body"]["points"][0]["x"] = -999

    def after(index, job):
        completed.append(copy.deepcopy(job))
        job["body"]["points"][0]["x"] = -888
        job["body"]["points"].clear()
        before_copies[index]["body"]["points"].clear()

    gateway["client"].execute_jobs(jobs, before_send=before, after_done=after)
    assert [call.kwargs["json"] for call in path_calls(gateway)] == [job["body"] for job in expected]
    assert completed == expected


def test_replacement_is_validated_and_copied_before_dispatch(gateway):
    replacement = path_job()
    replacement["body"]["points"][0]["x"] = 475
    expected = copy.deepcopy(replacement)
    gateway["on_path"] = lambda: replacement["body"]["points"].clear()
    after = Mock()
    gateway["client"].execute_jobs([path_job()], before_send=lambda *_: replacement, after_done=after)
    assert path_calls(gateway)[0].kwargs["json"] == expected["body"]
    after.assert_called_once_with(0, expected)


def test_invalid_nested_replacement_blocks_dispatch(gateway):
    def before(_index, job):
        job["body"]["points"][1]["linear"] = 1
        return job

    with pytest.raises(RobotCommandError, match="Pre-command check failed"):
        gateway["client"].execute_jobs([path_job()], before_send=before)
    assert path_calls(gateway) == []
    assert gateway["post"].call_args_list[-1].args[0].endswith("/control/release")


def test_validate_and_copy_jobs_isolate_nested_points_and_dependencies():
    original = {**path_job(), "after": ["left_grip"]}
    validated = RobotClient._validate_job(original)
    copied = RobotClient._copy_job(validated)
    original["body"]["points"][0]["x"] = -1
    original["after"].clear()
    copied["body"]["points"][0]["x"] = -2
    copied["body"]["points"].pop()
    copied["after"].clear()
    assert validated == {**path_job(), "after": ["left_grip"]}


@pytest.mark.parametrize("schema", (None, {}, schema_fixture(), schema_fixture(blended_path=True)))
def test_path_dispatch_never_requires_or_fetches_discovery(gateway, schema):
    gateway["client"]._operator_gateway = parse_gateway_capabilities(schema)
    gateway["client"].execute_jobs([path_job()])
    gateway["get"].assert_not_called()
    assert len(path_calls(gateway)) == 1


def test_failed_discovery_refresh_is_informational_for_paths_and_manual_controls(gateway):
    gateway["get"].side_effect = requests.Timeout("discovery unavailable")
    capabilities = gateway["client"].refresh_operator_capabilities()
    assert all(capability["supported"] for capability in capabilities.values())
    gateway["client"].require_control_action("home")
    gateway["client"].execute_jobs([path_job()])
    assert len(path_calls(gateway)) == 1
    gateway["get"].assert_called_once()


def test_fresh_client_dispatches_without_discovery(gateway):
    gateway["client"].execute_jobs([path_job()])
    gateway["get"].assert_not_called()
    assert len(path_calls(gateway)) == 1


def test_callback_cannot_change_the_planned_endpoint_to_a_path(gateway):
    open_job = {"name": "left_open", "endpoint": RobotClient.GRIPPER_ENDPOINT,
                "body": {"arm": "left", "wait": True, "action": "open"}}
    with pytest.raises(RobotCommandError, match="cannot change a blended path endpoint"):
        gateway["client"].execute_jobs([open_job], before_send=lambda *_: path_job())
    gateway["get"].assert_not_called()
    assert path_calls(gateway) == []
    assert len(gateway["post"].call_args_list) == 2  # Acquire/release only.


def test_callback_cannot_silently_replace_path_with_individual_move(gateway):
    replacement = {"name": "fallback", "endpoint": RobotClient.MOVE_ENDPOINT,
                   "body": {"arm": "left", "wait": True,
                            **{name: path_job()["body"]["points"][0][name]
                               for name in ("x", "y", "z", "rx", "ry", "rz")}}}
    with pytest.raises(RobotCommandError, match="cannot change a blended path endpoint"):
        gateway["client"].execute_jobs([path_job()], before_send=lambda *_: replacement)
    assert path_calls(gateway) == []
    assert len(gateway["post"].call_args_list) == 2


@pytest.mark.parametrize("z", (198.9, 700.1))
def test_path_height_is_checked_only_at_dispatch_after_previous_jobs(gateway, z):
    later = path_job()
    later["body"]["points"][0]["z"] = z
    with pytest.raises(RobotCommandError, match="ordinary motion requires"):
        gateway["client"].execute_jobs([path_job(), later])
    assert len(path_calls(gateway)) == 1


@pytest.mark.parametrize("z", (199, 700))
def test_path_height_boundaries_are_allowed(gateway, z):
    job = path_job()
    job["body"]["points"][0]["z"] = z
    gateway["client"].execute_jobs([job])
    assert len(path_calls(gateway)) == 1


def test_v4_height_rule_allows_lower_pick_place_and_defers_xy_to_gateway():
    client = RobotClient("http://offline.invalid", {}, dry_run=True)
    for name in ("descend_to_cup", "replace_cup", "right_place"):
        job = {"name": name, "endpoint": RobotClient.MOVE_ENDPOINT,
               "body": {"arm": "left", "wait": True, "x": 1000, "y": -700, "z": 100,
                        "rx": 180, "ry": 0, "rz": 180}}
        client._check_motion_height(job)
        job["body"]["z"] = 701
        with pytest.raises(RobotCommandError, match="ordinary motion requires"):
            client._check_motion_height(job)
