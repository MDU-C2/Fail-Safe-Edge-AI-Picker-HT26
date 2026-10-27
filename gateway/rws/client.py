import html
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator

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
        with self._lock:
            if reset_program_pointer:
                with self.mastership():
                    self.post(
                        "/rw/rapid/execution",
                        params={"action": "resetpp"},
                    )

            self.post(
                "/rw/rapid/execution",
                params={"action": "start"},
                data=START_ARGUMENTS,
            )

    def stop(self) -> None:
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
