"""The events WebSocket (development mode only)."""

import json
import time
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from gateway.tests.support import Gateway, expect


RECEIVE_TIMEOUT_SECONDS = 5.0


def open_events(
    gateway: Gateway,
    client_id: str,
) -> ClientConnection:
    url = gateway.url.replace("http://", "ws://")

    return connect(f"{url}/api/v1/events?client={client_id}")


def request(
    websocket: ClientConnection,
    action: str,
    events: Any,
) -> dict:
    websocket.send(json.dumps({"action": action, "events": events}))

    return json.loads(websocket.recv(timeout=RECEIVE_TIMEOUT_SECONDS))


def wait_for(
    websocket: ClientConnection,
    event_type: str,
) -> dict:
    """Skip other events until one of event_type arrives."""

    deadline = time.monotonic() + RECEIVE_TIMEOUT_SECONDS

    while True:
        remaining = deadline - time.monotonic()

        assert remaining > 0, f"No {event_type} event received"

        message = json.loads(websocket.recv(timeout=remaining))

        if message["type"] == event_type:
            return message


def test_subscribe_and_receive_by_prefix(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        reply = request(websocket, "subscribe", ["control"])

        assert reply == {
            "type": "subscription.updated",
            "data": {"events": ["control"]},
        }

        operator = development_gateway.client("operator")

        expect(operator.post("/control/acquire"), 200)

        event = wait_for(websocket, "control.acquired")

        assert event["data"]["client_id"] == "operator"

        expect(operator.post("/control/release"), 200)

        assert wait_for(websocket, "control.released")["data"]["client_id"] == "operator"


def test_unsubscribe(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        request(websocket, "subscribe", ["control", "calibration"])

        reply = request(websocket, "unsubscribe", ["control"])

        assert reply["data"]["events"] == ["calibration"]


def test_unknown_event_rejects_whole_request(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        reply = request(websocket, "subscribe", ["control", "banana"])

        assert reply == {
            "type": "subscription.rejected",
            "data": {"events": [{"event": "banana", "reason": "unknown_event"}]},
        }

        #
        # control was not subscribed either.
        #
        assert request(websocket, "unsubscribe", [])["data"]["events"] == []


@pytest.mark.parametrize(
    "message",
    [
        json.dumps({"action": "subscribe", "events": "control"}),
        json.dumps({"action": "subscribe", "events": [1]}),
        json.dumps({"action": "dance", "events": []}),
        json.dumps(["subscribe", "control"]),
        "subscribe control",
    ],
)
def test_malformed_requests(
    development_gateway: Gateway,
    message: str,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        websocket.send(message)

        reply = json.loads(websocket.recv(timeout=RECEIVE_TIMEOUT_SECONDS))

        assert reply["type"] == "error"

        #
        # The connection is still usable afterwards.
        #
        assert request(websocket, "subscribe", ["control"])["type"] == "subscription.updated"


def test_preempted_client_is_told_without_subscribing(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "autonomy") as websocket:
        expect(development_gateway.client("autonomy").post("/control/acquire"), 200)
        expect(development_gateway.client("operator").post("/control/acquire"), 200)

        event = wait_for(websocket, "control.revoked")

        assert event["data"] == {"reason": "preempted", "new_controller": "operator"}


def test_calibration_event(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        request(websocket, "subscribe", ["calibration"])

        operator = development_gateway.client("operator")

        with operator.control():
            expect(operator.post("/calibration", json={"units": "mm"}), 200)

        assert wait_for(websocket, "calibration.updated")["data"]["updated_by"] == "operator"


def test_unknown_development_client_is_refused(
    development_gateway: Gateway,
) -> None:
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        with open_events(development_gateway, "intruder") as websocket:
            websocket.recv(timeout=RECEIVE_TIMEOUT_SECONDS)


def test_strict_mode_refuses_websocket(
    strict_gateway: Gateway,
) -> None:
    #
    # Token auth is not implemented for the WebSocket yet, so
    # strict mode closes every connection.
    #
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        with open_events(strict_gateway, "operator") as websocket:
            websocket.recv(timeout=RECEIVE_TIMEOUT_SECONDS)
