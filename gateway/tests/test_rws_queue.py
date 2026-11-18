"""The RWS motion queue (POST|GET|DELETE /robot/rws/queue) on the
mock gateways: what the gateway enforces before contacting the
controller. Running queues, lease changes, cancel and failure are
in test_robotstudio.py; the queue itself in
gateway/cpp/tests/test_rws_queue.cpp.

C++ gateway only: the Python gateway is legacy.
"""

import pytest

from gateway.tests.support import Gateway, expect
from gateway.tests.test_events import open_events, request


pytestmark = pytest.mark.cpp_only

IDLE = {
    "state": "idle",
    "owner": None,
    "length": 0,
    "step": None,
    "cancel_requested": False,
    "message": None,
    "started_at": None,
    "finished_at": None,
    "steps": [],
}

HOME = {"name": "home", "type": "home"}

MOVE = {"name": "approach", "type": "move", "body": {"x": 400, "y": 0, "z": 300}}


def test_queue_needs_permission_and_lease(
    gateway: Gateway,
) -> None:
    diagnostics = gateway.client("diagnostics")
    primary = gateway.client("primary")

    #
    # Reading needs robot.read only, no lease.
    #
    assert expect(diagnostics.get("/robot/rws/queue"), 200) == IDLE

    expect(diagnostics.post("/robot/rws/queue", json={"steps": [HOME]}), 403)
    expect(diagnostics.delete("/robot/rws/queue"), 403)

    #
    # Starting, cancelling and clearing need the lease.
    #
    expect(primary.post("/robot/rws/queue", json={"steps": [HOME]}), 409)
    expect(primary.delete("/robot/rws/queue"), 409)

    with primary.control():
        #
        # Idle: nothing to cancel or clear.
        #
        assert expect(primary.delete("/robot/rws/queue"), 200) == IDLE


def test_queue_is_validated_before_contacting_the_controller(
    strict_gateway: Gateway,
) -> None:
    primary = strict_gateway.client("primary")

    def locations(
        body: dict,
    ) -> list[list]:
        detail = expect(primary.post("/robot/rws/queue", json=body), 422)["detail"]

        return [error["loc"] for error in detail]

    with primary.control():
        assert locations({}) == [["body", "steps"]]
        assert locations({"steps": []}) == [["body", "steps"]]
        assert locations({"steps": [HOME] * 65}) == [["body", "steps"]]

        assert locations({"steps": [{"type": "home"}]}) == [["body", "steps", 0, "name"]]
        assert locations({"steps": [{"name": "x", "type": "jump"}]}) == [["body", "steps", 0, "type"]]
        assert locations({"steps": ["home"]}) == [["body", "steps", 0]]

        #
        # A job body follows the rules of its single endpoint.
        #
        assert locations({"steps": [HOME, {"name": "move", "type": "move"}]}) == [
            ["body", "steps", 1, "body"],
        ]

        assert locations({"steps": [{"name": "m", "type": "move", "body": {"x": 1, "y": 2}}]}) == [
            ["body", "steps", 0, "body", "z"],
        ]

        assert locations(
            {"steps": [{"name": "m", "type": "move", "body": {"x": 1, "y": 2, "z": 3, "rx": 0}}]}
        ) == [["body", "steps", 0, "body"]]

        assert locations(
            {"steps": [{"name": "g", "type": "gripper", "body": {"action": "move"}}]}
        ) == [["body", "steps", 0, "body"]]

        assert locations(
            {"steps": [{"name": "p", "type": "path", "body": {"points": [{"x": 1, "y": 2, "z": 3}]}}]}
        ) == [
            ["body", "steps", 0, "body", "points", 0, "rx"],
            ["body", "steps", 0, "body", "points", 0, "ry"],
            ["body", "steps", 0, "body", "points", 0, "rz"],
        ]

        assert locations({"steps": [{**HOME, "body": {"arm": "middle"}}]}) == [
            ["body", "steps", 0, "body", "arm"],
        ]

        #
        # Every target in the safe zone, checked for the whole queue
        # before anything runs.
        #
        outside = {"name": "far", "type": "move", "body": {"x": 2000, "y": 0, "z": 300}}

        detail = expect(primary.post("/robot/rws/queue", json={"steps": [MOVE, outside]}), 422)["detail"]

        assert detail[0]["loc"] == ["body", "steps"]
        assert "Job 2 (far)" in detail[0]["msg"]
        assert "safe zone" in detail[0]["msg"]

    assert expect(primary.get("/robot/rws/queue"), 200) == IDLE


def test_queue_needs_a_reachable_controller(
    strict_gateway: Gateway,
) -> None:
    primary = strict_gateway.client("primary")

    with primary.control():
        #
        # The mock gateways' RWS port is closed: refused before the
        # queue starts, so nothing is left to clear.
        #
        expect(primary.post("/robot/rws/queue", json={"steps": [HOME, MOVE]}), 503)

    assert expect(primary.get("/robot/rws/queue"), 200) == IDLE


def test_queue_events_can_be_subscribed(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        reply = request(websocket, "subscribe", ["robot.queue"])

        assert reply == {
            "type": "subscription.updated",
            "data": {"events": ["robot.queue"]},
        }
