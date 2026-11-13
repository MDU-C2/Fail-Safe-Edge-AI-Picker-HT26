"""
TLS between clients and the gateway (gateway/cpp/TLS.md).

Every test gateway already serves HTTPS / WSS with the test CA
(support.tls_files()), so the rest of the suite covers the API over
TLS. These tests cover what is specific to TLS: which clients get
through, old protocol versions, and the C++ gateway refusing to start
in strict mode without a usable certificate.
"""

import socket
import ssl
from pathlib import Path

import pytest
import requests

from gateway.tests.conftest import mock_settings
from gateway.tests.support import Gateway, expect, tls_files

#
# Only the C++ gateway serves TLS (support.Gateway).
#
pytestmark = pytest.mark.cpp_only

# ================================================================
# Helpers
# ================================================================

def start_fails(
    directory: Path,
    settings: dict[str, str],
) -> str:
    """Start a strict gateway with these settings, expect it to exit
    during startup, and return its log."""

    gateway = Gateway(
        directory,
        "strict",
        {**mock_settings(), **settings},
        tls=False,
    )

    try:
        with pytest.raises(RuntimeError, match="exited during startup") as failure:
            gateway.start()

    finally:
        gateway.stop()

    return str(failure.value)


# ================================================================
# Clients
# ================================================================

def test_client_trusting_the_ca_gets_through(
    strict_gateway: Gateway,
) -> None:
    assert strict_gateway.url.startswith("https://")

    expect(strict_gateway.client("diagnostics").get("/control"), 200)


def test_client_trusting_another_ca_is_refused(
    strict_gateway: Gateway,
) -> None:
    with pytest.raises(requests.exceptions.SSLError, match="CERTIFICATE_VERIFY_FAILED"):
        requests.get(
            f"{strict_gateway.api_url}/health",
            verify=str(tls_files().other_ca),
            timeout=10,
        )


def test_certificate_must_name_the_address(
    strict_gateway: Gateway,
) -> None:
    """The test certificate is for 127.0.0.1 / localhost only."""

    context = ssl.create_default_context(cafile=str(tls_files().ca))

    with socket.create_connection(("127.0.0.1", strict_gateway.port), timeout=10) as raw:
        with pytest.raises(ssl.SSLCertVerificationError, match="mismatch"):
            context.wrap_socket(raw, server_hostname="gateway.example")


@pytest.mark.filterwarnings("ignore:ssl.TLSVersion:DeprecationWarning")
def test_tls_1_1_is_refused(
    strict_gateway: Gateway,
) -> None:
    context = ssl.create_default_context(cafile=str(tls_files().ca))

    context.minimum_version = ssl.TLSVersion.TLSv1
    context.maximum_version = ssl.TLSVersion.TLSv1_1

    try:
        #
        # Python's OpenSSL normally refuses TLS < 1.2 itself; allow it
        # here so it is the gateway that says no.
        #
        context.set_ciphers("DEFAULT:@SECLEVEL=0")

    except ssl.SSLError:
        pytest.skip("this client's OpenSSL cannot offer TLS 1.1")

    with socket.create_connection(("127.0.0.1", strict_gateway.port), timeout=10) as raw:
        with pytest.raises(ssl.SSLError) as refused:
            context.wrap_socket(raw, server_hostname="127.0.0.1")

    if "NO_PROTOCOLS_AVAILABLE" in str(refused.value):
        pytest.skip("this client's OpenSSL cannot offer TLS 1.1")


# ================================================================
# Plain HTTP (C++ gateway)
# ================================================================

def test_health_says_whether_tls_is_on(
    strict_gateway: Gateway,
) -> None:
    assert expect(strict_gateway.client(None).get("/health"), 200)["tls"] == {"enabled": True}


def test_development_mode_may_run_without_tls(
    tmp_path: Path,
) -> None:
    gateway = Gateway(tmp_path, "development", mock_settings(), tls=False)

    gateway.start()

    try:
        health = expect(gateway.client("diagnostics").get("/health"), 200)

        assert health["tls"] == {"enabled": False}

    finally:
        gateway.stop()


def test_strict_mode_without_tls_only_when_allowed(
    tmp_path: Path,
) -> None:
    refused = tmp_path / "refused"
    refused.mkdir()

    log = start_fails(refused, {"GATEWAY_TLS_ALLOW_PLAINTEXT": "false"})

    assert "Strict mode needs TLS" in log

    gateway = Gateway(
        tmp_path,
        "strict",
        {**mock_settings(), "GATEWAY_TLS_ALLOW_PLAINTEXT": "true"},
        tls=False,
    )

    gateway.start()

    try:
        expect(gateway.client("diagnostics").get("/control"), 200)

        assert "tokens cross the network unencrypted" in gateway.log()

    finally:
        gateway.stop()


# ================================================================
# Unusable certificates (C++ gateway)
# ================================================================

def other_key(directory: Path) -> Path:
    """The key of a certificate other than the gateway's."""

    import trustme

    path = directory / "other.key"

    trustme.CA().issue_cert("127.0.0.1").private_key_pem.write_to_path(str(path))

    return path


def encrypted_key(directory: Path) -> Path:
    """The gateway's key, encrypted: OpenSSL must not sit waiting for
    a passphrase on a terminal the device does not have."""

    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(tls_files().key.read_bytes(), password=None)

    path = directory / "encrypted.key"

    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"secret"),
        )
    )

    return path


@pytest.mark.parametrize(
    "make_key, expected",
    [
        (lambda directory: directory / "missing.key", "no such file"),
        (other_key, "cannot use GATEWAY_TLS_CERT_FILE"),
        (encrypted_key, "cannot use GATEWAY_TLS_CERT_FILE"),
    ],
    ids=["missing", "another-certificates-key", "encrypted"],
)
def test_unusable_key_stops_startup(
    tmp_path: Path,
    make_key,
    expected: str,
) -> None:
    log = start_fails(
        tmp_path,
        {
            "GATEWAY_TLS_CERT_FILE": str(tls_files().certificate),
            "GATEWAY_TLS_KEY_FILE": str(make_key(tmp_path)),
        },
    )

    assert expected in log


def test_certificate_without_key_stops_startup(
    tmp_path: Path,
) -> None:
    log = start_fails(tmp_path, {"GATEWAY_TLS_CERT_FILE": str(tls_files().certificate)})

    assert "must be set together" in log
