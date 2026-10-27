"""The control lease: acquire, renew, release, preemption, expiry."""

import time

from gateway.tests.support import LEASE_SECONDS, Gateway, expect


def test_no_lease_initially(
    gateway: Gateway,
) -> None:
    assert expect(gateway.client().get("/control"), 200) == {
        "active": False,
        "client_id": None,
        "priority": None,
        "acquired_at": None,
        "expires_at": None,
    }


def test_acquire_and_release(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    result = expect(operator.post("/control/acquire"), 200)

    assert result["success"]
    assert result["lease"]["client_id"] == "operator"
    assert result["lease"]["priority"] == 100

    lease = expect(gateway.client("diagnostics").get("/control"), 200)

    assert lease["active"] and lease["client_id"] == "operator"

    expect(operator.post("/control/release"), 200)

    assert not expect(operator.get("/control"), 200)["active"]


def test_acquire_twice_is_rejected(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    expect(operator.post("/control/acquire"), 200)
    expect(operator.post("/control/acquire"), 409)


def test_renew_extends_lease(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    acquired = expect(operator.post("/control/acquire"), 200)["lease"]

    time.sleep(0.2)

    renewed = expect(operator.post("/control/renew"), 200)["lease"]

    assert renewed["acquired_at"] == acquired["acquired_at"]
    assert renewed["expires_at"] > acquired["expires_at"]


def test_only_owner_can_renew_or_release(
    gateway: Gateway,
) -> None:
    autonomy = gateway.client("autonomy")

    expect(autonomy.post("/control/renew"), 409)
    expect(autonomy.post("/control/release"), 409)

    expect(gateway.client("operator").post("/control/acquire"), 200)

    expect(autonomy.post("/control/renew"), 409)
    expect(autonomy.post("/control/release"), 409)


def test_higher_priority_preempts(
    gateway: Gateway,
) -> None:
    autonomy = gateway.client("autonomy")
    operator = gateway.client("operator")

    expect(autonomy.post("/control/acquire"), 200)
    expect(operator.post("/control/acquire"), 200)

    assert expect(operator.get("/control"), 200)["client_id"] == "operator"

    expect(autonomy.post("/control/renew"), 409)


def test_lower_priority_cannot_preempt(
    gateway: Gateway,
) -> None:
    expect(gateway.client("operator").post("/control/acquire"), 200)
    expect(gateway.client("autonomy").post("/control/acquire"), 409)


def test_lease_expires_without_renewal(
    gateway: Gateway,
) -> None:
    autonomy = gateway.client("autonomy")

    expect(autonomy.post("/control/acquire"), 200)

    time.sleep(LEASE_SECONDS + 0.5)

    assert not expect(autonomy.get("/control"), 200)["active"]

    expect(autonomy.post("/control/renew"), 409)


def test_renewal_keeps_control_past_lease_period(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        time.sleep(LEASE_SECONDS * 2)

        lease = expect(operator.get("/control"), 200)

        assert lease["active"] and lease["client_id"] == "operator"
