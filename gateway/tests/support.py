"""
Helpers for running the gateway as a separate process in tests.

Each Gateway is a real uvicorn process on a free local port with
its own auth clients file and calibration file in a temporary
directory, so tests never touch gateway/calibration.json or the
tokens in gateway/auth_clients.json. Settings are passed as
GATEWAY_* environment variables, which take precedence over
gateway/.env.

C++ gateways serve HTTPS / WSS by default, with a certificate from a
throwaway CA made once per test run (trustme), never the lab CA of
gateway/cpp/TLS.md; Gateway(..., tls=False) starts a plain one. The
legacy Python gateway stays plain HTTP: it ships no TLS, and its
suite runs on Windows, where the Python TLS WebSocket client hangs
after ~100 connections (seen on the arm64 laptop's x64 Python 3.14).
"""

import atexit
import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Iterator

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

API_PREFIX = "/api/v1"

#
# Which gateway the tests start: "python" (uvicorn gateway.main:app)
# or "cpp" (the C++ binary at CPP_BINARY, relative to the repository
# root). Set with --impl / GATEWAY_IMPL and --cpp-binary /
# GATEWAY_CPP_BINARY; conftest.py fills these in.
#
IMPLEMENTATIONS = ("python", "cpp")

implementation = os.environ.get("GATEWAY_IMPL", "python")

cpp_binary = os.environ.get("GATEWAY_CPP_BINARY", "gateway/cpp/build/dev/gateway")

STARTUP_TIMEOUT_SECONDS = 90.0

#
# RobotStudio virtual controller: RWS, and the team's RAPID socket
# server (rapid/CommServer.mod) when a test loads it.
#
ROBOTSTUDIO_RWS_PORT = 80
ROBOTSTUDIO_SOCKET_PORT = 5001

#
# Short leases keep the expiry tests fast. Clients that hold
# control renew well inside this period.
#
LEASE_SECONDS = 2.0
RENEW_INTERVAL_SECONDS = 0.5

#
# A free lease goes to the highest client that tried within this
# window. Shorter than the lease, longer than the failover tests'
# device tick. No startup grace by default, so tests can take
# control as soon as a gateway is up; test_failover.py starts its
# own gateway with one.
#
CONTENTION_SECONDS = 1.0

#
# Same clients and permissions as gateway/auth_clients.json, with
# tokens known to the tests.
#
CLIENTS = {
    "admin": {
        "permissions": [
            "*",
        ],
        "control_priority": 1000,
    },
    "primary": {
        "permissions": [
            "robot.read",
            "robot.control",
            "camera.read",
        ],
        "control_priority": 300,
    },
    "secondary": {
        "permissions": [
            "robot.read",
            "robot.control",
            "camera.read",
        ],
        "control_priority": 200,
    },
    "tertiary": {
        "permissions": [
            "robot.read",
            "robot.control",
            "camera.read",
        ],
        "control_priority": 100,
    },
    "diagnostics": {
        "permissions": [
            "robot.read",
            "camera.read",
        ],
        "control_priority": 10,
    },
}


def token_for(
    client_id: str,
) -> str:
    return f"test-token-{client_id}"


# ================================================================
# TLS
# ================================================================

@dataclass(frozen=True)
class TlsFiles:
    """PEM files of the test CA and the test gateways' certificate."""

    ca: Path
    certificate: Path
    key: Path

    #
    # A second CA, which the gateways do not use: a client trusting
    # only this one must refuse them.
    #
    other_ca: Path


@cache
def tls_files() -> TlsFiles:
    """The test CA and a certificate for 127.0.0.1 / localhost,
    made on first use and deleted when the run ends."""

    import trustme

    directory = Path(tempfile.mkdtemp(prefix="gateway-test-tls-"))

    atexit.register(shutil.rmtree, directory, ignore_errors=True)

    ca = trustme.CA()
    certificate = ca.issue_cert("127.0.0.1", "localhost")

    files = TlsFiles(
        ca=directory / "ca.pem",
        certificate=directory / "gateway.crt",
        key=directory / "gateway.key",
        other_ca=directory / "other-ca.pem",
    )

    ca.cert_pem.write_to_path(str(files.ca))

    #
    # Only the gateway's own certificate, as lab_ca.sh issues it.
    #
    certificate.cert_chain_pems[0].write_to_path(str(files.certificate))
    certificate.private_key_pem.write_to_path(str(files.key))

    trustme.CA().cert_pem.write_to_path(str(files.other_ca))

    return files


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


def write_auth_clients(
    path: Path,
) -> None:
    clients = {
        client_id: {
            **client,
            "token_hash": hashlib.sha256(
                token_for(client_id).encode()
            ).hexdigest(),
        }
        for client_id, client in CLIENTS.items()
    }

    path.write_text(
        json.dumps(clients, indent=4),
        encoding="utf-8",
    )


# ================================================================
# Clients
# ================================================================

class Client:
    """HTTP client for one gateway identity.

    In strict mode it sends the client's Bearer token, in
    development mode the X-Dev-Client header. anonymous sends
    neither.
    """

    def __init__(
        self,
        gateway: "Gateway",
        client_id: str | None,
    ) -> None:
        self.gateway = gateway
        self.client_id = client_id

        self.session = requests.Session()
        self.session.verify = gateway.verify

        if client_id is None:
            return

        if gateway.security_mode == "strict":
            self.session.headers["Authorization"] = (
                f"Bearer {token_for(client_id)}"
            )

        else:
            self.session.headers["X-Dev-Client"] = client_id

    def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", 180)

        return self.session.request(
            method,
            f"{self.gateway.api_url}{path}",
            **kwargs,
        )

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    @contextmanager
    def control(self) -> Iterator[None]:
        """Hold the control lease, renewed from a thread."""

        expect(self.post("/control/acquire"), 200)

        stop = threading.Event()
        lost = threading.Event()

        def renew() -> None:
            while not stop.wait(RENEW_INTERVAL_SECONDS):
                if self.post("/control/renew").status_code != 200:
                    lost.set()
                    return

        thread = threading.Thread(
            target=renew,
            daemon=True,
        )

        thread.start()

        try:
            yield

        finally:
            stop.set()
            thread.join()

            self.post("/control/release")

        assert not lost.is_set(), "Control lease was lost while held"


class RemoteGateway:
    """A gateway the tests did not start, e.g. the one on the
    Raspberry Pi, reached with a token from its own auth clients
    file."""

    security_mode = "strict"

    def __init__(
        self,
        api_url: str,
        token: str,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token

        #
        # An https gateway is checked against the lab CA
        # (gateway/cpp/TLS.md), or GATEWAY_CA_FILE.
        #
        self.verify: bool | str = os.environ.get(
            "GATEWAY_CA_FILE",
            str(REPOSITORY_ROOT / "gateway" / "tls" / "lab-ca.crt"),
        )

        if not Path(self.verify).is_file():
            self.verify = True

    def client(self) -> Client:
        client = Client(self, None)

        client.session.headers["Authorization"] = f"Bearer {self.token}"

        return client


def expect(
    response: requests.Response,
    status_code: int,
) -> Any:
    """Assert the status code and return the JSON body, if any."""

    assert response.status_code == status_code, (
        f"{response.request.method} {response.url}: "
        f"expected {status_code}, got {response.status_code}: "
        f"{response.text[:500]}"
    )

    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()

    return None


# ================================================================
# Gateway process
# ================================================================

class Gateway:
    def __init__(
        self,
        directory: Path,
        security_mode: str,
        settings: dict[str, str],
        implementation: str | None = None,
        tls: bool | None = None,
    ) -> None:
        self.security_mode = security_mode
        self.port = free_port()

        #
        # The suite's implementation (--impl) unless one is given,
        # for tests that start both.
        #
        self.implementation = implementation

        #
        # HTTPS with the test CA's certificate, or plain HTTP.
        #
        if tls is None:
            tls = self.selected_implementation() == "cpp"

        self.tls = tls_files() if tls else None

        scheme = "https" if tls else "http"

        self.url = f"{scheme}://127.0.0.1:{self.port}"
        self.api_url = f"{self.url}{API_PREFIX}"
        self.ws_url = f"{'wss' if tls else 'ws'}://127.0.0.1:{self.port}"

        #
        # requests' verify: the test CA for HTTPS.
        #
        self.verify: bool | str = str(self.tls.ca) if self.tls else True

        self.log_path = directory / "gateway.log"

        auth_clients = directory / "auth_clients.json"

        write_auth_clients(auth_clients)

        self.environment = {
            **os.environ,
            "GATEWAY_SECURITY_MODE": security_mode,
            "GATEWAY_AUTH_CLIENTS_FILE": str(auth_clients),
            "GATEWAY_CALIBRATION_FILE": str(directory / "calibration.json"),
            "GATEWAY_CONTROL_LEASE_SECONDS": str(LEASE_SECONDS),
            "GATEWAY_CONTROL_CONTENTION_SECONDS": str(CONTENTION_SECONDS),
            "GATEWAY_CONTROL_STARTUP_GRACE_SECONDS": "0",
            **settings,
        }

        if self.tls:
            #
            # The C++ gateway reads these; the Python one gets them
            # as uvicorn options (command()).
            #
            self.environment.setdefault("GATEWAY_TLS_CERT_FILE", str(self.tls.certificate))
            self.environment.setdefault("GATEWAY_TLS_KEY_FILE", str(self.tls.key))

        elif security_mode == "strict":
            self.environment.setdefault("GATEWAY_TLS_ALLOW_PLAINTEXT", "true")

        self._process: subprocess.Popen | None = None

    def ssl_context(self) -> ssl.SSLContext | None:
        """For websockets' connect(ssl=...): trusts the test CA."""

        if not self.tls:
            return None

        return ssl.create_default_context(cafile=str(self.tls.ca))

    def connect_websocket(
        self,
        path: str,
        **kwargs: Any,
    ) -> "ClientConnection":
        """websockets' connect() to path over WS / WSS.

        Retried once when the opening handshake gets no answer: the
        websockets client over TLS 1.3 loses one in a few hundred
        handshakes, with this gateway and with websockets' own
        server alike. A refusal (an HTTP status) is not retried.
        """

        from websockets.exceptions import InvalidMessage
        from websockets.sync.client import connect

        for attempt in (1, 2):
            try:
                return connect(
                    f"{self.ws_url}{path}",
                    ssl=self.ssl_context(),
                    open_timeout=10,
                    **kwargs,
                )

            except (InvalidMessage, TimeoutError):
                if attempt == 2:
                    raise

    def selected_implementation(self) -> str:
        """This gateway's implementation, else the suite's (--impl)."""

        return self.implementation or implementation

    def command(self) -> list[str]:
        """The command line of the selected implementation."""

        if self.selected_implementation() == "cpp":
            return [
                str(REPOSITORY_ROOT / cpp_binary),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ]

        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]

        if self.tls:
            command += [
                "--ssl-certfile",
                self.environment["GATEWAY_TLS_CERT_FILE"],
                "--ssl-keyfile",
                self.environment["GATEWAY_TLS_KEY_FILE"],
            ]

        return command

    def start(self) -> None:
        self._log = self.log_path.open("w", encoding="utf-8")

        self._process = subprocess.Popen(
            self.command(),
            cwd=REPOSITORY_ROOT,
            env=self.environment,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"Gateway exited during startup:\n{self.log()}"
                )

            try:
                if requests.get(f"{self.api_url}/health", timeout=5, verify=self.verify).ok:
                    return

            except requests.RequestException:
                pass

            time.sleep(0.2)

        self.stop()

        raise RuntimeError(
            f"Gateway did not start within {STARTUP_TIMEOUT_SECONDS:g} s:\n{self.log()}"
        )

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

            try:
                self._process.wait(timeout=15)

            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

        self._process = None

        self._log.close()

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def client(
        self,
        client_id: str | None = "admin",
    ) -> Client:
        return Client(self, client_id)

    def release_all(self) -> None:
        """Release a lease left behind by a test."""

        for client_id in CLIENTS:
            self.client(client_id).post("/control/release")
