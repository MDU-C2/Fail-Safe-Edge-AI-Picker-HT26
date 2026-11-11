"""
Read-only diagnostics for the Robot Gateway: a terminal dashboard,
a one-shot status report, the event stream and raw GETs.

It acts as the `diagnostics` client (robot.read + camera.read), so it
can look at everything and change nothing: no lease, no moves, no
uploads.

Usage (from the repository root):
    python diagnostics/gateway_diag.py watch                  # live dashboard + events
    python diagnostics/gateway_diag.py --target pi watch
    python diagnostics/gateway_diag.py status                 # snapshot, exit 0 ok / 1 degraded / 2 unreachable
    python diagnostics/gateway_diag.py status --json
    python diagnostics/gateway_diag.py events [--events control robot] [--json]
    python diagnostics/gateway_diag.py get /robot/arms/left/joints

Identity:
    strict mode       Authorization: Bearer <token>; the token comes from
                      --token, else GATEWAY_DIAG_TOKEN, else the
                      "diagnostics" entry of client_tokens.json at the
                      repository root (local only, git-ignored).
    development mode  X-Dev-Client: <--client> (default diagnostics);
                      the WebSocket uses ?client=<--client>.

Events come from WS /api/v1/events: in strict mode with the same Bearer
token in the upgrade request (without a token the dashboard polls only),
in development mode as ?client=<--client>.

Works against either implementation (Python or C++).

Needs: requests, websockets, rich (diagnostics/requirements.txt)
"""

import argparse
import json
import math
import os
import ssl
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect


# ================================================================
# Defaults
# ================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGETS = {
    "local": "http://127.0.0.1:8000/api/v1",
    "pi": "http://169.254.125.202:8000/api/v1",
    "lynx": "http://169.254.219.127:8000/api/v1",
}

ARMS = ("left", "right")

#
# Every event family in gateway/events/permissions.py; diagnostics
# may subscribe to all of them.
#
EVENT_FAMILIES = [
    "control",
    "robot.status",
    "robot.error",
    "robot.rapid",
    "camera.status",
    "calibration",
]

EVENT_STYLES = {
    "control.revoked": "bold red",
    "control": "cyan",
    "robot.error": "bold red",
    "robot.status": "green",
    "robot.rapid": "magenta",
    "camera.status": "yellow",
    "calibration": "blue",
    "subscription": "dim",
    "error": "red",
}


# ================================================================
# HTTP
# ================================================================

@dataclass
class Result:
    path: str
    status: int | None = None
    data: Any = None
    error: str | None = None
    elapsed_ms: float = 0.0

    #
    # Server time minus local time, from the Date header (1 s
    # resolution), so lease expiry can be shown without trusting that
    # the Pi or the Lynx has the right clock.
    #
    clock_offset: float | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def describe_failure(self) -> str:
        if self.status is None:
            return self.error or "no response"

        detail = self.data.get("detail") if isinstance(self.data, dict) else None

        return f"HTTP {self.status}" + (f": {detail}" if detail else "")

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


class Gateway:
    def __init__(
        self,
        url: str,
        token: str | None,
        client: str,
        verify: bool,
        timeout: float,
    ) -> None:
        self.url = url.rstrip("/")
        self.client = client
        self.verify = verify
        self.timeout = timeout
        self.token = token or None
        self.has_token = bool(token)

        self.session = requests.Session()
        self.session.verify = verify

        #
        # Both headers go on every request: the gateway uses whichever
        # its security mode needs and ignores the other.
        #
        self.session.headers["X-Dev-Client"] = client

        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(
        self,
        path: str,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> Result:
        result = Result(path=path)
        started = time.monotonic()

        try:
            response = self.session.get(
                f"{self.url}{path}",
                params=params,
                timeout=timeout or self.timeout,
            )

        except requests.RequestException as error:
            result.error = short_error(error)
            result.elapsed_ms = (time.monotonic() - started) * 1000
            return result

        result.elapsed_ms = (time.monotonic() - started) * 1000
        result.status = response.status_code

        try:
            result.data = response.json()

        except ValueError:
            result.data = response.text[:500] or None

        date = response.headers.get("Date")

        if date:
            try:
                result.clock_offset = (
                    parsedate_to_datetime(date) - datetime.now(timezone.utc)
                ).total_seconds()

            except (TypeError, ValueError):
                pass

        return result

    def events_url(self) -> str:
        if self.url.startswith("https://"):
            return "wss://" + self.url[len("https://"):] + "/events"

        return "ws://" + self.url.removeprefix("http://") + "/events"


def short_error(error: Exception) -> str:
    text = str(error)

    for marker in ("Failed to establish a new connection: ", "Read timed out", "Connection refused"):
        if marker in text:
            return "unreachable (" + text.split(marker, 1)[-1].split("'))")[0].strip(" ()'\"") + ")"

    return text[:160]


# ================================================================
# Snapshot
# ================================================================

def collect(
    gateway: Gateway,
) -> dict[str, Result]:
    """
    One round of every read-only endpoint. RWS-backed endpoints are
    skipped while /health says the controller is unreachable, so a
    dead controller costs one probe instead of six timeouts.
    """

    results = {"health": gateway.get("/health")}

    if not results["health"].ok:
        return results

    requests_to_make: dict[str, tuple[str, dict | None]] = {
        "control": ("/control", None),
        "camera": ("/camera/status", None),
        "robot": ("/robot/status", None),
        "calibration": ("/calibration", None),
    }

    controller = (results["health"].data or {}).get("robot_controller") or {}

    if controller.get("reachable"):
        requests_to_make["rapid"] = ("/robot/rapid", None)
        requests_to_make["arms"] = ("/robot/arms", None)

        for arm in ARMS:
            requests_to_make[f"gripper.{arm}"] = (f"/robot/arms/{arm}/gripper", None)
            requests_to_make[f"job.{arm}"] = ("/robot/rws/status", {"arm": arm})

    with ThreadPoolExecutor(max_workers=len(requests_to_make)) as pool:
        futures = {
            name: pool.submit(gateway.get, path, params)
            for name, (path, params) in requests_to_make.items()
        }

        for name, future in futures.items():
            results[name] = future.result()

    return results


def exit_code(
    results: dict[str, Result],
) -> int:
    health = results["health"]

    if not health.ok:
        return 2

    return 0 if (health.data or {}).get("status") == "ok" else 1


# ================================================================
# Formatting helpers
# ================================================================

def yes_no(
    value: Any,
    good: bool = True,
) -> Text:
    if value is None:
        return Text("-", style="dim")

    ok = bool(value) == good

    return Text("yes" if value else "no", style="green" if ok else "red")


def state_text(
    value: str | None,
    good: tuple[str, ...] = (),
    bad: tuple[str, ...] = (),
) -> Text:
    if value is None:
        return Text("-", style="dim")

    style = "green" if value in good else "red" if value in bad else "yellow"

    return Text(str(value), style=style)


def parse_time(
    value: str | None,
) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    except ValueError:
        return None


def local_clock(
    moment: datetime | None,
) -> str:
    return "-" if moment is None else moment.astimezone().strftime("%H:%M:%S")


def duration(
    seconds: float | None,
) -> str:
    if seconds is None:
        return "-"

    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if days:
        return f"{days}d {hours}h {minutes}m"

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def quaternion_to_zyx(
    pose: dict,
) -> tuple[float, float, float]:
    """rx, ry, rz in degrees, the meaning of RAPID OrientZYX(rz, ry, rx) (as in rws_move.py)."""

    w, x, y, z = (pose[name] for name in ("q1", "q2", "q3", "q4"))

    rx = math.atan2(2 * (y * z + w * x), 1 - 2 * (x * x + y * y))
    ry = math.asin(max(-1.0, min(1.0, 2 * (w * y - x * z))))
    rz = math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))

    return tuple(math.degrees(angle) + 0.0 for angle in (rx, ry, rz))


def failure(
    result: Result | None,
    skipped: str = "skipped (controller unreachable)",
) -> Text:
    if result is None:
        return Text(skipped, style="dim")

    return Text(result.describe_failure(), style="red")


def grid() -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()

    return table


# ================================================================
# Panels
# ================================================================

@dataclass
class View:
    """What the dashboard remembers between polls."""

    intrinsics: Result | None = None
    previous_sequence: tuple[int, float] | None = None
    fps: float | None = None
    polled_at: datetime | None = None
    poll_ms: float = 0.0
    errors: int = 0

    def update_fps(
        self,
        camera: Result | None,
    ) -> None:
        sequence = (camera.data or {}).get("sequence") if camera and camera.ok else None

        if sequence is None:
            self.previous_sequence = None
            self.fps = None
            return

        now = time.monotonic()

        if self.previous_sequence is not None:
            last_sequence, last_time = self.previous_sequence

            if sequence >= last_sequence and now > last_time:
                self.fps = (sequence - last_sequence) / (now - last_time)

        self.previous_sequence = (sequence, now)


def header_panel(
    gateway: Gateway,
    results: dict[str, Result],
    view: View,
) -> Panel:
    health = results["health"]
    line = Text()

    if not health.ok:
        line.append(" UNREACHABLE ", style="bold white on red")
        line.append(f"  {gateway.url}  ")
        line.append(health.describe_failure(), style="red")

        return Panel(line, box=box.HEAVY, border_style="red")

    data = health.data or {}
    status = data.get("status", "?")

    line.append(f" {status.upper()} ", style="bold white on green" if status == "ok" else "bold black on yellow")
    line.append(f"  {gateway.url}", style="bold")
    line.append(f"   v{data.get('version', '?')}   up {duration(data.get('uptime_seconds'))}")
    line.append(f"   mode {data.get('security_mode', '?')}")
    line.append(f"   robot provider {data.get('robot_provider', '?')}")
    line.append("\n")

    #
    # A rejected token shows up on every protected route; /control is
    # the cheapest one to judge it by.
    #
    control = results.get("control")
    strict = data.get("security_mode") == "strict"

    if control is not None and control.status in (401, 403):
        line.append("AUTH REJECTED", style="bold red")

        if not gateway.has_token and strict:
            line.append(" (no token: --token / GATEWAY_DIAG_TOKEN / client_tokens.json)", style="red")

    else:
        line.append(
            "as token holder" if strict else f"as {gateway.client}",
            style="dim",
        )

    line.append(
        f"   polled {local_clock(view.polled_at)} in {view.poll_ms:.0f} ms",
        style="dim",
    )

    if view.errors:
        line.append(f"   {view.errors} failed requests", style="yellow")

    line.no_wrap = True
    line.overflow = "ellipsis"

    return Panel(line, box=box.HEAVY, border_style="green" if status == "ok" else "yellow")


def camera_panel(
    results: dict[str, Result],
    view: View,
) -> Panel:
    table = grid()
    health = (results["health"].data or {}).get("camera") or {}
    camera = results.get("camera")
    status = camera.data if camera is not None and camera.ok else {}

    table.add_row("provider", str(health.get("provider", "-")))
    table.add_row("connected", yes_no(health.get("connected")))

    age = health.get("frame_age_seconds")
    limit = health.get("max_frame_age_seconds")

    table.add_row(
        "frame age",
        Text(
            "-" if age is None else f"{age:.2f} s (max {limit} s)",
            style="dim" if age is None else "green" if limit is None or age <= limit else "red",
        ),
    )

    if camera is not None and not camera.ok:
        table.add_row("status", failure(camera))

    elif status:
        table.add_row("sequence", str(status.get("sequence", "-")))
        table.add_row("fps", "-" if view.fps is None else f"{view.fps:.1f}")
        table.add_row(
            "rgb",
            f"{status.get('rgb_width')}x{status.get('rgb_height')} {status.get('rgb_format') or ''}"
            if status.get("rgb_available")
            else Text("not available", style="red"),
        )
        table.add_row(
            "depth",
            f"{status.get('depth_width')}x{status.get('depth_height')} {status.get('depth_format') or ''}"
            if status.get("depth_available")
            else Text("not available", style="red"),
        )

    intrinsics = view.intrinsics

    if intrinsics is not None and intrinsics.ok:
        k = intrinsics.data
        table.add_row(
            "intrinsics",
            f"fx {k['fx']:.1f} fy {k['fy']:.1f} cx {k['cx']:.1f} cy {k['cy']:.1f}",
        )

    elif intrinsics is not None:
        table.add_row("intrinsics", failure(intrinsics))

    error = health.get("error") or status.get("error")

    if error:
        table.add_row("error", Text(str(error), style="red"))

    return Panel(
        table,
        title="camera",
        title_align="left",
        border_style="green" if health.get("ok") else "red",
    )


def control_panel(
    results: dict[str, Result],
) -> Panel:
    table = grid()
    control = results.get("control")

    if control is None or not control.ok:
        table.add_row("lease", failure(control, "-"))

    else:
        lease = control.data or {}

        if lease.get("active"):
            table.add_row("holder", Text(str(lease.get("client_id")), style="bold cyan"))
            table.add_row("priority", str(lease.get("priority")))
            table.add_row("acquired", local_clock(parse_time(lease.get("acquired_at"))))

            expires = parse_time(lease.get("expires_at"))
            remaining = None

            if expires is not None and control.clock_offset is not None:
                server_now = datetime.now(timezone.utc).timestamp() + control.clock_offset
                remaining = expires.timestamp() - server_now

            table.add_row(
                "expires",
                local_clock(expires) + ("" if remaining is None else f"  (~{max(remaining, 0):.0f} s)"),
            )

        else:
            table.add_row("holder", Text("nobody (lease free)", style="yellow"))

    calibration = results.get("calibration")

    if calibration is not None and calibration.ok:
        stored = calibration.data or {}
        updated = parse_time(stored.get("updated_at"))

        table.add_row(
            "calibration",
            f"{updated.astimezone():%Y-%m-%d %H:%M} by {stored.get('updated_by')}"
            if updated
            else "stored",
        )

    elif calibration is not None and calibration.status == 404:
        table.add_row("calibration", Text("none uploaded", style="yellow"))

    elif calibration is not None:
        table.add_row("calibration", failure(calibration))

    return Panel(table, title="control & calibration", title_align="left", border_style="cyan")


def robot_panel(
    results: dict[str, Result],
) -> Panel:
    table = grid()
    controller = (results["health"].data or {}).get("robot_controller") or {}

    table.add_row("controller", yes_no(controller.get("reachable")))
    table.add_row(
        "motors",
        state_text(controller.get("controller_state"), good=("motoron",), bad=("motoroff", "guardstop", "emergencystop", "sysfail")),
    )
    table.add_row(
        "mode",
        state_text(controller.get("operation_mode"), good=("AUTO",)),
    )
    table.add_row(
        "RAPID",
        state_text(controller.get("rapid_execution_state"), good=("running",), bad=("stopped",)),
    )
    table.add_row("gateway move", yes_no(controller.get("move_in_progress"), good=False))

    for arm in ARMS:
        job = results.get(f"job.{arm}")

        if job is not None and job.ok:
            message = (job.data or {}).get("message", "?")
            table.add_row(
                f"job {arm}",
                state_text(message, good=("DONE",), bad=("ERR move failed",)),
            )

        elif job is not None:
            table.add_row(f"job {arm}", failure(job))

    legacy = results.get("robot")

    if legacy is not None and legacy.ok:
        status = legacy.data or {}
        table.add_row(
            "legacy socket",
            Text.assemble(
                yes_no(status.get("connected")),
                f"  {status.get('state', '')}",
            ),
        )

    if controller.get("error"):
        table.add_row("error", Text(str(controller["error"]), style="red"))

    return Panel(
        table,
        title="robot",
        title_align="left",
        border_style="green" if controller.get("ok") else "red",
    )


def arms_panel(
    results: dict[str, Result],
) -> Panel:
    arms = results.get("arms")

    if arms is None or not arms.ok:
        return Panel(failure(arms), title="arms (mm, deg)", title_align="left")

    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)

    for column in ("arm", "x", "y", "z", "rx", "ry", "rz", "cf", "gripper", "tool / wobj"):
        table.add_column(column, justify="left" if column in ("arm", "cf", "tool / wobj") else "right", no_wrap=True)

    for arm in ARMS:
        pose = (arms.data or {}).get(arm)

        if not pose:
            table.add_row(arm, Text("no pose", style="red"))
            continue

        rx, ry, rz = quaternion_to_zyx(pose)
        gripper = results.get(f"gripper.{arm}")

        if gripper is not None and gripper.ok:
            gripper_text = Text(f"{gripper.data['position']:.1f} mm")

        elif gripper is not None and gripper.status == 404:
            gripper_text = Text("none", style="dim")

        else:
            gripper_text = failure(gripper, "-")

        table.add_row(
            arm,
            f"{pose['x']:.1f}",
            f"{pose['y']:.1f}",
            f"{pose['z']:.1f}",
            f"{rx:.1f}",
            f"{ry:.1f}",
            f"{rz:.1f}",
            f"{pose['cf1']},{pose['cf4']},{pose['cf6']},{pose['cfx']}",
            gripper_text,
            f"{pose['tool']} / {pose['wobj']}",
        )

    return Panel(table, title="arms (mm, deg)", title_align="left")


def rapid_panel(
    results: dict[str, Result],
) -> Panel:
    rapid = results.get("rapid")

    if rapid is None or not rapid.ok:
        return Panel(failure(rapid), title="RAPID tasks", title_align="left")

    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    table.add_column("task", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("exec", no_wrap=True)
    table.add_column("modules")

    for task in (rapid.data or {}).get("tasks", []):
        table.add_row(
            Text(task["name"], style="bold" if task.get("active") else "dim"),
            task.get("state", ""),
            state_text(task.get("execution_state"), good=("started",), bad=("stopped",)),
            ", ".join(task.get("modules", [])),
        )

    return Panel(table, title="RAPID tasks", title_align="left")


# ================================================================
# Events
# ================================================================

@dataclass
class ReceivedEvent:
    received_at: datetime
    message: dict


@dataclass
class EventFeed:
    """
    Background WebSocket subscriber that reconnects on its own. It
    sends ?client= (development mode) and, with a token, the Bearer
    header (strict mode); the gateway uses whichever its mode needs.
    """

    url: str
    client: str
    events: list[str]
    token: str | None = None
    verify: bool = True
    events_received: deque = field(default_factory=lambda: deque(maxlen=500))
    state: str = "not started"
    wake: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def ssl_context(self) -> ssl.SSLContext | None:
        if not self.url.startswith("wss://"):
            return None

        context = ssl.create_default_context()

        if not self.verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        return context

    def headers(self) -> dict | None:
        return {"Authorization": f"Bearer {self.token}"} if self.token else None

    def run(self) -> None:
        while not self.stop.is_set():
            self.state = "connecting"

            try:
                with connect(
                    f"{self.url}?client={self.client}",
                    open_timeout=5,
                    ssl=self.ssl_context(),
                    additional_headers=self.headers(),
                ) as websocket:
                    websocket.send(json.dumps({"action": "subscribe", "events": self.events}))
                    self.state = "connected"

                    while not self.stop.is_set():
                        try:
                            raw = websocket.recv(timeout=1)

                        except TimeoutError:
                            continue

                        try:
                            message = json.loads(raw)

                        except ValueError:
                            message = {"type": "error", "data": {"message": f"not JSON: {raw[:80]!r}"}}

                        self.events_received.append(ReceivedEvent(datetime.now(timezone.utc), message))
                        self.wake.set()

            except InvalidStatus as error:
                self.state = f"refused (HTTP {error.response.status_code}: missing or unknown token, or unknown --client)"

            except ConnectionClosed as error:
                code = error.rcvd.code if error.rcvd else None
                self.state = f"closed ({code})" if code else "closed"

            except (OSError, TimeoutError) as error:
                self.state = f"unreachable ({error})"[:120]

            self.stop.wait(5)


def event_style(
    event_type: str,
) -> str:
    for prefix in sorted(EVENT_STYLES, key=len, reverse=True):
        if event_type == prefix or event_type.startswith(prefix + "."):
            return EVENT_STYLES[prefix]

    return "white"


def event_summary(
    data: Any,
) -> str:
    if isinstance(data, dict):
        return "  ".join(
            f"{key}={json.dumps(value, separators=(',', ':')) if isinstance(value, (dict, list)) else value}"
            for key, value in data.items()
            if value is not None
        )

    return json.dumps(data)


def events_panel(
    feed: EventFeed | None,
    mode: str | None,
    height: int,
) -> Panel:
    if feed is None:
        reason = (
            "strict mode without a token: no events (give --token / GATEWAY_DIAG_TOKEN); polling only"
            if mode == "strict"
            else "waiting for the gateway"
        )

        return Panel(Text(reason, style="dim"), title="events", title_align="left")

    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(overflow="ellipsis", no_wrap=True)

    #
    # Panel border and title take two of the lines.
    #
    for event in list(feed.events_received)[-max(height - 2, 1):]:
        event_type = str(event.message.get("type", "?"))
        table.add_row(
            event.received_at.astimezone().strftime("%H:%M:%S.%f")[:-3],
            Text(event_type, style=event_style(event_type)),
            event_summary(event.message.get("data")),
        )

    if not feed.events_received:
        table.add_row("", Text("no events yet", style="dim"), "")

    return Panel(
        table,
        title=f"events ({feed.state}, {len(feed.events_received)} kept)",
        title_align="left",
        border_style="green" if feed.state == "connected" else "red",
    )


# ================================================================
# Commands
# ================================================================

def render_snapshot(
    gateway: Gateway,
    results: dict[str, Result],
    view: View,
) -> Group:
    if not results["health"].ok:
        return Group(header_panel(gateway, results, view))

    return Group(
        header_panel(gateway, results, view),
        camera_panel(results, view),
        robot_panel(results),
        control_panel(results),
        arms_panel(results),
        rapid_panel(results),
    )


def command_status(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    started = time.monotonic()
    results = collect(gateway)

    if results["health"].ok:
        results["intrinsics"] = gateway.get("/camera/intrinsics")

    if args.json:
        print(json.dumps({name: result.to_json() for name, result in results.items()}, indent=2))
        return exit_code(results)

    view = View(
        intrinsics=results.get("intrinsics"),
        polled_at=datetime.now(timezone.utc),
        poll_ms=(time.monotonic() - started) * 1000,
    )

    Console().print(render_snapshot(gateway, results, view))

    return exit_code(results)


def dashboard(
    gateway: Gateway,
    results: dict[str, Result],
    view: View,
    feed: "EventFeed | None",
    height: int,
) -> Layout:
    root = Layout()
    mode = (results["health"].data or {}).get("security_mode") if results["health"].ok else None
    events_height = min(max(height // 4, 6), 16)

    root.split_column(
        Layout(header_panel(gateway, results, view), name="header", size=4),
        Layout(name="body"),
        Layout(events_panel(feed, mode, events_height), name="events", size=events_height),
    )

    if not results["health"].ok:
        root["body"].update(Text(""))
        return root

    root["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=3),
    )
    root["left"].split_column(
        Layout(camera_panel(results, view), name="camera", size=11),
        Layout(robot_panel(results), name="robot"),
    )
    root["right"].split_column(
        Layout(arms_panel(results), name="arms", size=6),
        Layout(control_panel(results), name="control", size=6),
        Layout(rapid_panel(results), name="rapid"),
    )

    return root


def command_watch(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    console = Console()
    view = View()
    feed: EventFeed | None = None
    results: dict[str, Result] = {"health": Result(path="/health", error="not polled yet")}

    def layout() -> Layout:
        return dashboard(gateway, results, view, feed, console.size.height)

    try:
        with Live(layout(), console=console, screen=True, auto_refresh=False) as live:
            while True:
                started = time.monotonic()
                results = collect(gateway)
                view.polled_at = datetime.now(timezone.utc)
                view.poll_ms = (time.monotonic() - started) * 1000
                view.update_fps(results.get("camera"))
                view.errors += sum(1 for result in results.values() if result.status is None)

                health = results["health"]

                if health.ok:
                    #
                    # Intrinsics are fixed per camera connection; fetch
                    # them until they arrive, then keep them.
                    #
                    if view.intrinsics is None or not view.intrinsics.ok:
                        view.intrinsics = gateway.get("/camera/intrinsics")

                    mode = (health.data or {}).get("security_mode")

                    if feed is None and not args.no_events and (mode == "development" or (mode == "strict" and gateway.has_token)):
                        feed = EventFeed(
                            url=gateway.events_url(),
                            client=gateway.client,
                            events=args.events,
                            token=gateway.token,
                            verify=gateway.verify,
                        )
                        feed.start()

                live.update(layout(), refresh=True)

                #
                # Sleep until the next poll, redrawing whenever an
                # event arrives; an event also brings the next poll
                # forward so the panels catch up with it.
                #
                deadline = started + args.interval

                while (remaining := deadline - time.monotonic()) > 0:
                    if feed is not None and feed.wake.wait(min(remaining, 0.25)):
                        feed.wake.clear()
                        live.update(layout(), refresh=True)
                        deadline = min(deadline, time.monotonic() + 0.3)

                    elif feed is None:
                        time.sleep(min(remaining, 0.25))

    except KeyboardInterrupt:
        pass

    finally:
        if feed is not None:
            feed.stop.set()

    return 0


def command_events(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    console = Console(stderr=True)
    health = gateway.get("/health")

    if health.ok and (health.data or {}).get("security_mode") == "strict" and not gateway.has_token:
        console.print("[red]The gateway is in strict mode: its events WebSocket needs a token (--token, GATEWAY_DIAG_TOKEN or client_tokens.json).[/red]")
        return 2

    url = f"{gateway.events_url()}?client={gateway.client}"
    feed = EventFeed(url=gateway.events_url(), client=gateway.client, events=args.events, token=gateway.token, verify=gateway.verify)
    out = Console()

    try:
        with connect(url, open_timeout=5, ssl=feed.ssl_context(), additional_headers=feed.headers()) as websocket:
            websocket.send(json.dumps({"action": "subscribe", "events": args.events}))
            console.print(f"[dim]connected to {url}, subscribed to {' '.join(args.events)}[/dim]")

            for raw in websocket:
                if args.json:
                    print(raw, flush=True)
                    continue

                message = json.loads(raw)
                event_type = str(message.get("type", "?"))

                line = Text()
                line.append(datetime.now().strftime("%H:%M:%S.%f")[:-3] + " ", style="dim")
                line.append(event_type, style=event_style(event_type))
                line.append("  " + event_summary(message.get("data")))
                out.print(line, soft_wrap=True)

    except KeyboardInterrupt:
        return 0

    except InvalidStatus as error:
        console.print(f"[red]refused: HTTP {error.response.status_code} (missing or unknown token, or unknown --client)[/red]")
        return 2

    except (OSError, TimeoutError) as error:
        console.print(f"[red]cannot connect to {url}: {error}[/red]")
        return 2

    except ConnectionClosed as error:
        console.print(f"[red]connection closed: {error}[/red]")
        return 1

    return 0


def command_get(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    params = dict(pair.split("=", 1) for pair in args.param)
    path = args.path if args.path.startswith("/") else "/" + args.path
    result = gateway.get(path, params=params or None)

    if result.status is None:
        print(f"error: {result.error}", file=sys.stderr)
        return 2

    print(f"HTTP {result.status}  {result.elapsed_ms:.0f} ms", file=sys.stderr)
    print(json.dumps(result.data, indent=2) if not isinstance(result.data, str) else result.data)

    return 0 if result.ok else 1


# ================================================================
# Main
# ================================================================

def resolve_token(
    explicit: str | None,
) -> str | None:
    if explicit:
        return explicit

    if os.environ.get("GATEWAY_DIAG_TOKEN"):
        return os.environ["GATEWAY_DIAG_TOKEN"]

    try:
        tokens = json.loads((REPO_ROOT / "client_tokens.json").read_text(encoding="utf-8"))

    except (OSError, ValueError):
        return None

    entry = tokens.get("diagnostics") if isinstance(tokens, dict) else None

    if isinstance(entry, dict):
        return entry.get("token")

    return entry if isinstance(entry, str) else None


def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for the Robot Gateway (diagnostics client)",
    )

    parser.add_argument("--url", default=os.environ.get("GATEWAY_URL"), help="Gateway API base URL (default: GATEWAY_URL or --target)")
    parser.add_argument("--target", choices=sorted(TARGETS), default="local", help="Known gateway when --url is not given")
    parser.add_argument("--token", default=None, help="Bearer token (default: GATEWAY_DIAG_TOKEN, then client_tokens.json 'diagnostics')")
    parser.add_argument("--client", default="diagnostics", help="Client id in development mode")
    parser.add_argument("--timeout", type=float, default=5, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Do not verify TLS certificates (https/wss)")

    commands = parser.add_subparsers(dest="command")

    watch = commands.add_parser("watch", help="Live dashboard with the event log (default)")
    watch.add_argument("--interval", type=float, default=2, help="Seconds between polls")
    watch.add_argument("--events", nargs="+", default=EVENT_FAMILIES, help="Event names or prefixes")
    watch.add_argument("--no-events", action="store_true", help="Do not open the events WebSocket")

    status = commands.add_parser("status", help="One snapshot; exit 0 ok, 1 degraded, 2 unreachable")
    status.add_argument("--json", action="store_true", help="Raw results as JSON")

    events = commands.add_parser("events", help="Stream events (strict mode needs a token)")
    events.add_argument("--events", nargs="+", default=EVENT_FAMILIES, help="Event names or prefixes")
    events.add_argument("--json", action="store_true", help="One raw JSON message per line")

    get = commands.add_parser("get", help="GET one path with the diagnostics identity and print the JSON")
    get.add_argument("path", help="Path below the API base, e.g. /robot/arms/left/joints")
    get.add_argument("param", nargs="*", help="Query parameters as key=value")

    args = parser.parse_args(argv)

    if args.command is None:
        args = parser.parse_args([*(argv if argv is not None else sys.argv[1:]), "watch"])

    return args


def main(
    argv: list[str] | None = None,
) -> int:
    args = parse_args(argv)

    gateway = Gateway(
        url=args.url or TARGETS[args.target],
        token=resolve_token(args.token),
        client=args.client,
        verify=not args.insecure,
        timeout=args.timeout,
    )

    if args.insecure:
        requests.packages.urllib3.disable_warnings()

    commands = {
        "watch": command_watch,
        "status": command_status,
        "events": command_events,
        "get": command_get,
    }

    return commands[args.command](gateway, args)


if __name__ == "__main__":
    sys.exit(main())
