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
    admin = gateway.client("admin")

    result = expect(admin.post("/control/acquire"), 200)

    assert result["success"]
    assert result["lease"]["client_id"] == "admin"
    assert result["lease"]["priority"] == 1000

    lease = expect(gateway.client("diagnostics").get("/control"), 200)

    assert lease["active"] and lease["client_id"] == "admin"

    expect(admin.post("/control/release"), 200)

    assert not expect(admin.get("/control"), 200)["active"]


def test_acquire_twice_is_rejected(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    expect(admin.post("/control/acquire"), 200)
    expect(admin.post("/control/acquire"), 409)


def test_renew_extends_lease(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    acquired = expect(admin.post("/control/acquire"), 200)["lease"]

    time.sleep(0.2)

    renewed = expect(admin.post("/control/renew"), 200)["lease"]

    assert renewed["acquired_at"] == acquired["acquired_at"]
    assert renewed["expires_at"] > acquired["expires_at"]


def test_only_owner_can_renew_or_release(
    gateway: Gateway,
) -> None:
    primary = gateway.client("primary")

    expect(primary.post("/control/renew"), 409)
    expect(primary.post("/control/release"), 409)

    expect(gateway.client("admin").post("/control/acquire"), 200)

    expect(primary.post("/control/renew"), 409)
    expect(primary.post("/control/release"), 409)


def test_higher_priority_preempts(
    gateway: Gateway,
) -> None:
    primary = gateway.client("primary")
    admin = gateway.client("admin")

    expect(primary.post("/control/acquire"), 200)
    expect(admin.post("/control/acquire"), 200)

    assert expect(admin.get("/control"), 200)["client_id"] == "admin"

    expect(primary.post("/control/renew"), 409)


def test_lower_priority_cannot_preempt(
    gateway: Gateway,
) -> None:
    expect(gateway.client("admin").post("/control/acquire"), 200)
    expect(gateway.client("primary").post("/control/acquire"), 409)


def test_lease_expires_without_renewal(
    gateway: Gateway,
) -> None:
    primary = gateway.client("primary")

    expect(primary.post("/control/acquire"), 200)

    time.sleep(LEASE_SECONDS + 0.5)

    assert not expect(primary.get("/control"), 200)["active"]

    expect(primary.post("/control/renew"), 409)


def test_renewal_keeps_control_past_lease_period(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    with admin.control():
        time.sleep(LEASE_SECONDS * 2)

        lease = expect(admin.get("/control"), 200)

        assert lease["active"] and lease["client_id"] == "admin"


def test_device_tiers_fall_back_and_take_back_control(
    gateway: Gateway,
) -> None:
    """Primary > secondary > tertiary; a lower tier gets control
    only once the higher one's lease has lapsed, and a higher tier
    takes it back as soon as it returns."""

    primary = gateway.client("primary")
    secondary = gateway.client("secondary")
    tertiary = gateway.client("tertiary")

    expect(primary.post("/control/acquire"), 200)
    expect(secondary.post("/control/acquire"), 409)
    expect(tertiary.post("/control/acquire"), 409)

    time.sleep(LEASE_SECONDS + 0.5)

    expect(secondary.post("/control/acquire"), 200)
    expect(tertiary.post("/control/acquire"), 409)

    time.sleep(LEASE_SECONDS + 0.5)

    expect(tertiary.post("/control/acquire"), 200)
    expect(secondary.post("/control/acquire"), 200)
    expect(primary.post("/control/acquire"), 200)

    assert expect(primary.get("/control"), 200)["client_id"] == "primary"

    expect(gateway.client("admin").post("/control/acquire"), 200)

