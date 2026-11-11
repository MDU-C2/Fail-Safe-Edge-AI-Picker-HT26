"""The events WebSocket (development mode: ?client=, strict mode: Bearer token)."""

import json
import time
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from gateway.tests.support import Gateway, expect, token_for


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

        admin = development_gateway.client("admin")

        expect(admin.post("/control/acquire"), 200)

        event = wait_for(websocket, "control.acquired")

        assert event["data"]["client_id"] == "admin"

        expect(admin.post("/control/release"), 200)

        assert wait_for(websocket, "control.released")["data"]["client_id"] == "admin"


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
    with open_events(development_gateway, "primary") as websocket:
        expect(development_gateway.client("primary").post("/control/acquire"), 200)
        expect(development_gateway.client("admin").post("/control/acquire"), 200)

        event = wait_for(websocket, "control.revoked")

        assert event["data"] == {"reason": "preempted", "new_controller": "admin"}


def test_calibration_event(
    development_gateway: Gateway,
) -> None:
    with open_events(development_gateway, "diagnostics") as websocket:
        request(websocket, "subscribe", ["calibration"])

        admin = development_gateway.client("admin")

        with admin.control():
            expect(admin.post("/calibration", json={"units": "mm"}), 200)

        assert wait_for(websocket, "calibration.updated")["data"]["updated_by"] == "admin"


def test_closed_connections_are_released(
    development_gateway: Gateway,
) -> None:
    """More connections than the gateway serves at once, one after
    the other: each must be freed when it closes."""

    for _ in range(100):
        with open_events(development_gateway, "diagnostics") as websocket:
            request(websocket, "subscribe", ["control"])

    admin = development_gateway.client("admin")

    expect(admin.get("/control"), 200)

    with open_events(development_gateway, "diagnostics") as websocket:
        request(websocket, "subscribe", ["control"])

        expect(admin.post("/control/acquire"), 200)

        assert wait_for(websocket, "control.acquired")["data"]["client_id"] == "admin"

        expect(admin.post("/control/release"), 200)


def test_unknown_development_client_is_refused(
    development_gateway: Gateway,
) -> None:
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        with open_events(development_gateway, "intruder") as websocket:
            websocket.recv(timeout=RECEIVE_TIMEOUT_SECONDS)


def open_strict_events(
    gateway: Gateway,
    authorization: str | None,
    query: str = "",
) -> ClientConnection:
    url = gateway.url.replace("http://", "ws://")
    headers = {"Authorization": authorization} if authorization is not None else None

    return connect(f"{url}/api/v1/events{query}", additional_headers=headers)


def test_strict_mode_with_token(
    strict_gateway: Gateway,
) -> None:
    #
    # The upgrade request carries the same Bearer token as the HTTP
    # routes; events then flow as in development mode.
    #
    authorization = f"Bearer {token_for('diagnostics')}"

    with open_strict_events(strict_gateway, authorization) as websocket:
        assert request(websocket, "subscribe", ["control", "calibration"])["type"] == "subscription.updated"

        admin = strict_gateway.client("admin")

        with admin.control():
            expect(admin.post("/calibration", json={"units": "mm"}), 200)

        assert wait_for(websocket, "control.acquired")["data"]["client_id"] == "admin"
        assert wait_for(websocket, "calibration.updated")["data"]["updated_by"] == "admin"
        assert wait_for(websocket, "control.released")["data"]["client_id"] == "admin"


def test_strict_mode_preempted_client_is_told(
    strict_gateway: Gateway,
) -> None:
    with open_strict_events(strict_gateway, f"Bearer {token_for('primary')}") as websocket:
        expect(strict_gateway.client("primary").post("/control/acquire"), 200)
        expect(strict_gateway.client("admin").post("/control/acquire"), 200)

        assert wait_for(websocket, "control.revoked")["data"] == {"reason": "preempted", "new_controller": "admin"}

        expect(strict_gateway.client("admin").post("/control/release"), 200)


@pytest.mark.parametrize(
    ("authorization", "query"),
    [
        (None, ""),
        (None, "?client=admin"),
        ("Bearer not-a-token", ""),
        ("Bearer ", ""),
        (f"Basic {token_for('admin')}", ""),
        (token_for("admin"), ""),
    ],
    ids=["no-token", "client-query-ignored", "unknown-token", "empty-token", "wrong-scheme", "no-scheme"],
)
def test_strict_mode_refuses_without_valid_token(
    strict_gateway: Gateway,
    authorization: str | None,
    query: str,
) -> None:
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        with open_strict_events(strict_gateway, authorization, query) as websocket:
            websocket.recv(timeout=RECEIVE_TIMEOUT_SECONDS)
