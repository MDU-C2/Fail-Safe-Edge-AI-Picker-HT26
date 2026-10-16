"""Calibration upload (needs the control lease) and read-back."""

from gateway.tests.support import Gateway, expect


CALIBRATION = {
    "camera_to_robot": [
        [1.0, 0.0, 0.0, 400.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 600.0],
        [0.0, 0.0, 0.0, 1.0],
    ],
    "units": "mm",
}


def test_upload_and_read_back(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        stored = expect(operator.post("/calibration", json=CALIBRATION), 200)

    assert stored["calibration"] == CALIBRATION
    assert stored["updated_by"] == "operator"

    #
    # Any client with camera.read can read it.
    #
    assert expect(gateway.client("diagnostics").get("/calibration"), 200) == stored


def test_upload_needs_lease(
    gateway: Gateway,
) -> None:
    expect(gateway.client("operator").post("/calibration", json=CALIBRATION), 409)


def test_upload_needs_lease_of_this_client(
    gateway: Gateway,
) -> None:
    with gateway.client("operator").control():
        expect(gateway.client("autonomy").post("/calibration", json=CALIBRATION), 409)


def test_read_only_client_cannot_upload(
    gateway: Gateway,
) -> None:
    expect(gateway.client("diagnostics").post("/calibration", json=CALIBRATION), 403)


def test_upload_must_be_json_object(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/calibration", json=[1, 2, 3]), 422)


def test_upload_size_limit(
    gateway: Gateway,
) -> None:
    operator = gateway.client("operator")

    with operator.control():
        expect(operator.post("/calibration", json={"blob": "x" * 1_000_001}), 413)


def test_calibration_needs_token(
    strict_gateway: Gateway,
) -> None:
    expect(strict_gateway.client(None).get("/calibration"), 401)
