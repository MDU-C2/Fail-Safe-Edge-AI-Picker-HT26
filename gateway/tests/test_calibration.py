"""Calibration upload (needs the control lease) and read-back."""

from pathlib import Path

import pytest

from gateway.tests import support
from gateway.tests.conftest import mock_settings
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
    admin = gateway.client("admin")

    with admin.control():
        stored = expect(admin.post("/calibration", json=CALIBRATION), 200)

    assert stored["calibration"] == CALIBRATION
    assert stored["updated_by"] == "admin"

    #
    # Any client with camera.read can read it.
    #
    assert expect(gateway.client("diagnostics").get("/calibration"), 200) == stored


def test_upload_needs_lease(
    gateway: Gateway,
) -> None:
    expect(gateway.client("admin").post("/calibration", json=CALIBRATION), 409)


def test_upload_needs_lease_of_this_client(
    gateway: Gateway,
) -> None:
    with gateway.client("admin").control():
        expect(gateway.client("primary").post("/calibration", json=CALIBRATION), 409)


def test_read_only_client_cannot_upload(
    gateway: Gateway,
) -> None:
    expect(gateway.client("diagnostics").post("/calibration", json=CALIBRATION), 403)


def test_upload_must_be_json_object(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    with admin.control():
        expect(admin.post("/calibration", json=[1, 2, 3]), 422)


def test_upload_size_limit(
    gateway: Gateway,
) -> None:
    admin = gateway.client("admin")

    with admin.control():
        expect(admin.post("/calibration", json={"blob": "x" * 1_000_001}), 413)


def test_calibration_needs_token(
    strict_gateway: Gateway,
) -> None:
    expect(strict_gateway.client(None).get("/calibration"), 401)


# ================================================================
# The calibration file
# ================================================================

def upload(
    directory: Path,
    implementation: str | None = None,
) -> dict:
    """Start a gateway on directory's calibration file, upload, stop."""

    gateway = Gateway(directory, "strict", mock_settings(), implementation)

    gateway.start()

    try:
        admin = gateway.client("admin")

        with admin.control():
            return expect(admin.post("/calibration", json=CALIBRATION), 200)

    finally:
        gateway.stop()


def read_back(
    directory: Path,
    implementation: str | None = None,
) -> dict:
    """Start a gateway on directory's calibration file, read it, stop."""

    gateway = Gateway(directory, "strict", mock_settings(), implementation)

    gateway.start()

    try:
        return expect(gateway.client("diagnostics").get("/calibration"), 200)

    finally:
        gateway.stop()


def test_calibration_survives_a_restart(
    tmp_path: Path,
) -> None:
    stored = upload(tmp_path)

    assert read_back(tmp_path) == stored


def test_python_and_cpp_read_each_others_calibration_file(
    tmp_path: Path,
) -> None:
    """The two implementations share the file format, so switching
    a device from one to the other keeps its calibration."""

    if support.implementation != "cpp":
        pytest.skip("needs --impl cpp (and the Python gateway's packages)")

    for writer, reader in (("python", "cpp"), ("cpp", "python")):
        directory = tmp_path / f"{writer}-to-{reader}"

        directory.mkdir()

        stored = upload(directory, writer)

        assert read_back(directory, reader) == stored, f"{writer} wrote, {reader} read"
