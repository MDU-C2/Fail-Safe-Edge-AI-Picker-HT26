import html
import re
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import requests
from requests.auth import HTTPDigestAuth

from gateway.config import Settings, get_settings


#
# Arguments for starting RAPID execution, the same as the
# FlexPendant's start button.
#
START_ARGUMENTS = {
    "regain": "continue",
    "execmode": "continue",
    "cycle": "forever",
    "condition": "none",
    "stopatbp": "disabled",
    "alltaskbytsp": "false",
}


#
# RWS error messages start with the controller's source file and
# line, e.g. "C:\BUILDAGENTS\...\rws_resource_rapid.cpp[1602] ERROR: ".
#
SOURCE_LOCATION = re.compile(
    r"[A-Za-z]:\\\S*?\.cpp\[\d+\]:?\s*(ERROR:\s*)?"
)

XHTML_MESSAGE = re.compile(
    r'<span class="msg">(.*?)</span>',
    re.DOTALL,
)


#
# Seconds, or (connect, read) seconds as requests takes them.
#
Timeout = float | tuple[float, float]


class RwsError(Exception):
    """A failed Robot Web Services call.

    status_code is the HTTP status returned by the controller, or
    None when the controller could not be reached at all.
    """

    def __init__(
        self,
        status_code: int | None,
        message: str,
    ) -> None:
        super().__init__(message)

        self.status_code = status_code
        self.message = message


class RwsClient:
    """Robot Web Services 1.0 client for an IRC5 controller.

    Checked against RobotWare 6.16. RobotWare 7 uses a different
    API and is not supported.

    All calls are blocking and serialised by a lock, so the
    session cookie is shared and mastership is never requested
    by two requests at once. Call it through asyncio.to_thread.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        timeout: float,
    ) -> None:
        self._base = f"http://{host}:{port}"
        self._timeout = timeout

        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth(user, password)

        self._lock = threading.RLock()
        self._start_stop_lock = threading.Lock()
        self._program_lock = threading.Lock()
        self._stop_generation_lock = threading.Lock()
        self._stop_generation = secrets.randbits(48)

    # ============================================================
    # HTTP
    # ============================================================

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        timeout: Timeout | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        #
        # json=1 makes RWS answer in JSON, including the reason in
        # error responses, instead of XHTML.
        #
        params = {
            **(params or {}),
            "json": "1",
        }

        with self._lock:
            try:
                response = self._session.request(
                    method,
                    f"{self._base}{path}",
                    params=params,
                    timeout=timeout or self._timeout,
                    **kwargs,
                )

            except requests.RequestException as error:
                raise RwsError(
                    None,
                    f"Robot controller unreachable at "
                    f"{self._base}: {_connection_problem(error)}",
                ) from error

        if response.status_code >= 400:
            raise RwsError(
                response.status_code,
                _error_message(response),
            )

        return response

    def get_state(
        self,
        path: str,
        params: dict[str, str] | None = None,
        timeout: Timeout | None = None,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            path,
            params=params,
            timeout=timeout,
        )

        return response.json()["_embedded"]["_state"]

    def post(
        self,
        path: str,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> None:
        #
        # RWS rejects a POST without this content type, even when
        # the body is empty.
        #
        self._request(
            "POST",
            path,
            params=params,
            data=data or {},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    @contextmanager
    def mastership(self) -> Iterator[None]:
        with self._lock:
            self.post(
                "/rw/mastership",
                params={"action": "request"},
            )

            try:
                yield

            finally:
                self.post(
                    "/rw/mastership",
                    params={"action": "release"},
                )

    # ============================================================
    # Motion system
    # ============================================================

    def mechunit(
        self,
        unit: str,
    ) -> dict[str, Any]:
        return self.get_state(
            f"/rw/motionsystem/mechunits/{unit}"
        )[0]

    def robtarget(
        self,
        unit: str,
        tool: str | None = None,
        wobj: str | None = None,
        coordinate: str | None = None,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in (
                ("tool", tool),
                ("wobj", wobj),
                ("coordinate", coordinate),
            )
            if value is not None
        }

        return self.get_state(
            f"/rw/motionsystem/mechunits/{unit}/robtarget",
            params,
        )[0]

    def jointtarget(
        self,
        unit: str,
    ) -> dict[str, Any]:
        return self.get_state(
            f"/rw/motionsystem/mechunits/{unit}/jointtarget"
        )[0]

    # ============================================================
    # Controller and RAPID state
    # ============================================================

    def controller_state(
        self,
        timeout: Timeout | None = None,
    ) -> str:
        return self.get_state("/rw/panel/ctrlstate", timeout=timeout)[0]["ctrlstate"]

    def operation_mode(
        self,
        timeout: Timeout | None = None,
    ) -> str:
        return self.get_state("/rw/panel/opmode", timeout=timeout)[0]["opmode"]

    def execution_state(
        self,
        timeout: Timeout | None = None,
    ) -> str:
        return self.get_state("/rw/rapid/execution", timeout=timeout)[0]["ctrlexecstate"]

    def collision_state(self) -> str:
        entry = self.get_state("/rw/panel/coldetstate")[0]
        value = next((value for key, value in entry.items() if key.strip() == "coldetstate"), None)
        if not isinstance(value, str) or value not in {"INIT", "TRIGGERED", "CONFIRMED", "TRIGGERED_ACK"}:
            raise RwsError(409, "Controller returned an unknown collision state")
        return value

    def acknowledge_collision(self, check_authorization: Callable[[], None] | None = None) -> str:
        """ABB requires Local Client privileges; a rejection is never bypassed.

        This acknowledges collision detection only, without motors-on or start.
        https://developercenter.robotstudio.com/api/rwsApi/panel_ack_set_page.html
        """
        if self.execution_state() != "stopped":
            raise RwsError(409, "Stop RAPID before acknowledging collision detection")
        state = self.collision_state()
        if state in {"INIT", "CONFIRMED"}:
            return state
        if check_authorization is not None:
            check_authorization()
        self.post("/rw/panel/opmode", params={"action": "acknowledge"}, data={"opmode": "coldet"})
        state = self.collision_state()
        if state not in {"INIT", "CONFIRMED"}:
            raise RwsError(409, "Collision acknowledgement is not confirmed; inspect the FlexPendant. No start or home was sent")
        return state

    def stop_generation(self) -> int:
        with self._stop_generation_lock:
            return self._stop_generation

    def check_stop_generation(self, generation: int) -> None:
        if self.stop_generation() != generation:
            raise RwsError(409, "A software stop interrupted program initialization; RAPID will not be started")

    def stop_for_reset(self) -> None:
        """Internal stop, without cancelling this reset's explicit start request."""
        self.post("/rw/rapid/execution", params={"action": "stop"}, data={"stopmode": "stop"})
        deadline = time.monotonic() + 10
        while (self.execution_state() != "stopped" or any(
                task.get("type") == "norm" and task.get("excstate") == "star"
                for task in self.tasks())):
            if time.monotonic() >= deadline:
                raise RwsError(409, "RAPID tasks did not stop within 10 seconds")
            time.sleep(0.05)

    def reset_program_pointer(self) -> None:
        with self.mastership():
            self.post("/rw/rapid/execution", params={"action": "resetpp"})

    @contextmanager
    def program_change(self) -> Iterator[None]:
        if not self._program_lock.acquire(blocking=False):
            raise RwsError(409, "Another RAPID initialization is already in progress")
        try:
            yield
        finally:
            self._program_lock.release()

    def start_fresh(self, generation: int, deadline: float,
                    check_authorization: Callable[[], None] | None = None) -> None:
        # A concurrent stop announces itself before waiting for this lock. If
        # start was already dispatched, that stop is posted after it completes.
        with self._start_stop_lock:
            self.check_stop_generation(generation)
            if self.operation_mode() != "AUTO" or self.controller_state() != "motoron":
                raise RwsError(409, "Fresh start requires AUTO mode and motors already on; use the controller controls")
            if self.collision_state() not in {"INIT", "CONFIRMED"}:
                raise RwsError(409, "Acknowledge the controller collision before starting RAPID")
            # Check after taking the HTTP lock: another read/mastership call
            # may have delayed dispatch while a Stop or lease expiry arrived.
            with self._lock:
                self.check_stop_generation(generation)
                if time.monotonic() >= deadline:
                    raise RwsError(409, "Program initialization exceeded its deadline; RAPID was not started")
                if check_authorization is not None:
                    check_authorization()
                self.post("/rw/rapid/execution", params={"action": "start"}, data=START_ARGUMENTS)
            self.check_stop_generation(generation)
            deadline = time.monotonic() + 3
            while self.execution_state() != "running":
                self.check_stop_generation(generation)
                if time.monotonic() >= deadline:
                    raise RwsError(409, "RAPID start was not confirmed; inspect the controller event log")
                time.sleep(0.05)

    def tasks(self) -> list[dict[str, Any]]:
        return self.get_state("/rw/rapid/tasks")

    def program_modules(
        self,
        task: str,
    ) -> list[str]:
        return [
            module["name"]
            for module in self.get_state(
                "/rw/rapid/modules",
                {"task": task},
            )
            if module.get("type") == "ProgMod"
        ]

    # ============================================================
    # RAPID data
    # ============================================================

    def read_symbol(
        self,
        task: str,
        name: str,
    ) -> str:
        """Return a RAPID variable as RAPID text, e.g. "TRUE",
        "[1,2,3]" or "\\"text\\"" (strings keep their quotes)."""

        return self.get_state(
            f"/rw/rapid/symbol/data/RAPID/{task}/{name}"
        )[0]["value"]

    def write_symbol(
        self,
        task: str,
        name: str,
        value: str,
    ) -> None:
        #
        # Works while RAPID runs and needs no mastership; the
        # controller refuses mastership while RAPID runs anyway.
        #
        self.post(
            f"/rw/rapid/symbol/data/RAPID/{task}/{name}",
            params={"action": "set"},
            data={"value": value},
        )

    # ============================================================
    # RAPID program control
    # ============================================================

    def load_module(
        self,
        task: str,
        name: str,
        source: str,
    ) -> None:
        #
        # The file is written to $HOME on the controller and then
        # loaded from there, replacing a module with the same name.
        #
        path = f"$HOME/{name}.mod"

        with self._lock, self.mastership():
            self._request(
                "PUT",
                f"/fileservice/{path}",
                data=source.encode("iso-8859-1"),
                headers={
                    "Content-Type": "text/plain;v=2.0",
                },
            )

            self.post(
                f"/rw/rapid/tasks/{task}",
                params={"action": "loadmod"},
                data={
                    "modulepath": path,
                    "replace": "true",
                },
            )

    def unload_module(
        self,
        task: str,
        name: str,
    ) -> None:
        with self._lock, self.mastership():
            self.post(
                f"/rw/rapid/tasks/{task}",
                params={"action": "unloadmod"},
                data={"module": name},
            )

    def start(
        self,
        reset_program_pointer: bool,
    ) -> None:
        generation = self.stop_generation()
        with self.program_change(), self._start_stop_lock, self._lock:
            self.check_stop_generation(generation)
            if reset_program_pointer:
                with self.mastership():
                    self.post(
                        "/rw/rapid/execution",
                        params={"action": "resetpp"},
                    )

            self.check_stop_generation(generation)
            self.post(
                "/rw/rapid/execution",
                params={"action": "start"},
                data=START_ARGUMENTS,
            )

    def stop(self) -> None:
        with self._stop_generation_lock:
            self._stop_generation += 1
        with self._start_stop_lock:
            self.post(
                "/rw/rapid/execution",
                params={"action": "stop"},
                data={"stopmode": "stop"},
            )


def _connection_problem(
    error: requests.RequestException,
) -> str:
    #
    # The requests messages repeat the URL and the urllib3 pool
    # internals; this is all a client needs to know.
    #
    if isinstance(error, requests.ConnectTimeout):
        return "connection timed out"

    if isinstance(error, requests.ReadTimeout):
        return "no answer in time"

    if isinstance(error, requests.ConnectionError):
        return "connection failed (refused or no route)"

    return type(error).__name__


def _error_message(
    response: requests.Response,
) -> str:
    #
    # RWS puts the reason in _embedded.status.msg. Some errors
    # (e.g. a badly formed RAPID value) come back as XHTML even
    # with json=1, with the reason in <span class="msg">. Fall
    # back to the raw text for anything else answering on the port.
    #
    try:
        message = response.json()["_embedded"]["status"]["msg"]

    except (ValueError, KeyError, TypeError):
        match = XHTML_MESSAGE.search(response.text)

        if match is None:
            return response.text[:300] or response.reason

        message = html.unescape(match.group(1))

    return SOURCE_LOCATION.sub("", message).strip()


def create_rws_client(
    settings: Settings,
) -> RwsClient:
    return RwsClient(
        host=settings.robot_host,
        port=settings.rws_port,
        user=settings.rws_user,
        password=settings.rws_password,
        timeout=settings.rws_timeout,
    )


rws_client = create_rws_client(
    get_settings()
)
