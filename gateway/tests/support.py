"""
Helpers for running the gateway as a separate process in tests.

Each Gateway is a real uvicorn process on a free local port with
its own auth clients file and calibration file in a temporary
directory, so tests never touch gateway/calibration.json or the
tokens in gateway/auth_clients.json. Settings are passed as
GATEWAY_* environment variables, which take precedence over
gateway/.env.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

API_PREFIX = "/api/v1"

STARTUP_TIMEOUT_SECONDS = 90.0

#
# RobotStudio virtual controller: RWS, and the RAPID socket server
# in gateway/rapid/LegacyCommServer.mod.
#
ROBOTSTUDIO_RWS_PORT = 80
ROBOTSTUDIO_SOCKET_PORT = 5000

#
# Short leases keep the expiry tests fast. Clients that hold
# control renew well inside this period.
#
LEASE_SECONDS = 2.0
RENEW_INTERVAL_SECONDS = 0.5

#
# Same clients and permissions as gateway/auth_clients.json, with
# tokens known to the tests.
#
CLIENTS = {
    "operator": {
        "permissions": [
            "robot.read",
            "robot.control",
            "camera.read",
            "camera.control",
        ],
        "control_priority": 100,
    },
    "autonomy": {
        "permissions": [
            "robot.read",
            "robot.control",
            "camera.read",
            "camera.control",
        ],
        "control_priority": 50,
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
    ) -> None:
        self.security_mode = security_mode
        self.port = free_port()

        self.url = f"http://127.0.0.1:{self.port}"
        self.api_url = f"{self.url}{API_PREFIX}"

        self.log_path = directory / "gateway.log"

        auth_clients = directory / "auth_clients.json"

        write_auth_clients(auth_clients)

        self.environment = {
            **os.environ,
            "GATEWAY_SECURITY_MODE": security_mode,
            "GATEWAY_AUTH_CLIENTS_FILE": str(auth_clients),
            "GATEWAY_CALIBRATION_FILE": str(directory / "calibration.json"),
            "GATEWAY_CONTROL_LEASE_SECONDS": str(LEASE_SECONDS),
            **settings,
        }

        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        self._log = self.log_path.open("w", encoding="utf-8")

        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "gateway.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
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
                if requests.get(f"{self.api_url}/health", timeout=1).ok:
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
        client_id: str | None = "operator",
    ) -> Client:
        return Client(self, client_id)

    def release_all(self) -> None:
        """Release a lease left behind by a test."""

        for client_id in CLIENTS:
            self.client(client_id).post("/control/release")
