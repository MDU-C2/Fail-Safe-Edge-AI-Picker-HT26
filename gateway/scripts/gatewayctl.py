"""
Everything an operator does with the Robot Gateway, in one tool:
robot jobs, the RAPID program, the camera, calibration, the control
lease, events, a smoke test and client tokens. It talks only to the
gateway, never to the robot or the camera directly.

    python gateway/scripts/gatewayctl.py [--target local|lynx|pi | --url URL] [--token T] COMMAND ...

Tokens: --token, else the GATEWAY_TOKEN environment variable (strict
mode; the plaintext tokens are in client_tokens.json, local only).
Each command needs a client with certain permissions; give the token
of one that has them (the gateway refuses the others, and the tool
says which permission was missing):

    command                          needs                              clients
    -------------------------------  ---------------------------------  ------------------------------
    status, arms, rapid status,      robot.read                         every client
      rapid backup, control show,
      events
    camera rgb|capture,              camera.read                        every client
      calibration get
    move, home, grip, open,          robot.control + the control lease  admin, primary, secondary,
      gripper-to, rapid start|stop|                                       tertiary, operator
      restart, calibration put
    rapid upload|delete|restore|     robot.program + the control lease  admin
      reset, reset-home
    control release                  any (releases its own lease)       every client
    smoke                            robot.control (or --read-only)
    token new                        nothing (offline)

Commands that need the lease take it, renew it while they run and
release it on exit. The admin (priority 1000) takes it from whichever
model holds it; the model gets it back once the admin releases.

Development mode: the gateway ignores tokens; --client picks the
client (X-Dev-Client), else the gateway uses the highest-priority one.

Examples:
    gatewayctl.py --target lynx status
    gatewayctl.py --token $T move --x 325 --y 200 --z 300 --speed 100
    gatewayctl.py move --arm right --rz 90                 # turn in place
    gatewayctl.py home --arm right ; gatewayctl.py grip --force 5 ; gatewayctl.py gripper-to 15
    gatewayctl.py rapid status
    gatewayctl.py rapid start --main                       # after a Stop or collision mid-job (acknowledge it on the FlexPendant first)
    gatewayctl.py rapid upload                             # back up, stop, load rapid/, start
    gatewayctl.py rapid upload --task T_ROB_L --no-start
    gatewayctl.py --target local rapid upload --gripper-stub   # RobotStudio only
    gatewayctl.py rapid reset                              # SharedVars' PERS to their declared values
    gatewayctl.py rapid backup ; gatewayctl.py rapid restore rapid/backups/<dir>
    gatewayctl.py reset-home                               # reset, both arms home, grippers closed
    gatewayctl.py camera capture --count 10 ; gatewayctl.py camera rgb -o frame.png
    gatewayctl.py calibration get -o calibration.json ; gatewayctl.py calibration put calibration.json
    gatewayctl.py control show ; gatewayctl.py control release
    gatewayctl.py events --events control robot.rapid
    gatewayctl.py smoke --read-only
    gatewayctl.py token new primary --priority 300

Robot jobs move whatever robot the gateway at --url drives; on the
Lynx and the Pi that is the real YuMi. Positions are tool0 in wobj0
in mm, rotations in degrees (RAPID OrientZYX(rz, ry, rx)); a value
left out is taken from the arm's current pose.

Needs: requests; websockets (events); numpy + opencv-python (camera)
"""

import argparse
import array
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import requests


# ================================================================
# Defaults
# ================================================================

REPO_ROOT = Path(__file__).resolve().parents[2]

RAPID_DIR = REPO_ROOT / "rapid"

#
# Git-ignored. GATEWAYCTL_BACKUP_DIR moves it (the tests do).
#
BACKUP_DIR = Path(os.environ.get("GATEWAYCTL_BACKUP_DIR", RAPID_DIR / "backups"))

SHARED_VARS = RAPID_DIR / "SharedVars.mod"

GRIPPER_STUB = RAPID_DIR / "sim" / "GripperStub.mod"

TARGETS = {
    "local": "http://127.0.0.1:8000/api/v1",
    "lynx": "http://169.254.219.127:8000/api/v1",
    "pi": "http://169.254.125.202:8000/api/v1",
}

#
# The program, as the .pgf files list it.
#
PROGRAM = {
    "T_ROB_L": ["SharedVars.mod", "LeftArmInstructions.mod", "GoHomeLeft.mod"],
    "T_ROB_R": ["SharedVars.mod", "RightArmInstructions.mod", "GoHomeRight.mod"],
    "communication": ["SharedVars.mod", "CommServer.mod"],
}

ARM_TASKS = ("T_ROB_L", "T_ROB_R")

ARMS = ("left", "right")

#
# Longer than the gateway's own move timeout (60 s).
#
JOB_TIMEOUT_SECONDS = 90

#
# Stopping waits up to 4 s, starting up to 8 s in the gateway.
#
RAPID_TIMEOUT_SECONDS = 60

#
# Every event family the gateway knows.
#
EVENTS = [
    "control",
    "robot.status",
    "robot.error",
    "robot.rapid",
    "camera.status",
    "calibration",
]

PERMISSIONS = {
    "read": "robot.read",
    "camera": "camera.read",
    "control": "robot.control and the control lease",
    "program": "robot.program (the admin) and the control lease",
}


# ================================================================
# Gateway
# ================================================================

class GatewayError(Exception):
    pass


class Gateway:
    def __init__(
        self,
        url: str,
        token: str | None,
        client: str | None,
        verify: str | bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token

        self._session = requests.Session()
        self._session.verify = verify

        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

        if client:
            self._session.headers["X-Dev-Client"] = client

    @property
    def headers(self) -> dict:
        return dict(self._session.headers)

    def request(
        self,
        method: str,
        path: str,
        needs: str,
        ok: tuple[int, ...] = (200,),
        timeout: float = 30,
        **kwargs,
    ) -> requests.Response:
        try:
            response = self._session.request(method, f"{self.url}{path}", timeout=timeout, **kwargs)

        except requests.RequestException as error:
            raise GatewayError(f"Gateway {self.url} unreachable: {type(error).__name__}") from error

        if response.status_code in ok:
            return response

        raise GatewayError(f"{method} {path}: {explain(response, PERMISSIONS[needs])}")

    def get(self, path: str, needs: str = "read", **kwargs) -> requests.Response:
        return self.request("GET", path, needs, **kwargs)

    def post(self, path: str, needs: str = "control", **kwargs) -> requests.Response:
        return self.request("POST", path, needs, **kwargs)

    # ---------------------------------------------------------------- lease

    @contextmanager
    def control(self) -> Iterator[None]:
        """Hold the control lease: acquire (waiting while the gateway
        asks to retry), renew in a thread, release on the way out."""

        holder = self.get("/control").json()

        deadline = time.monotonic() + 15

        while True:
            try:
                response = self._session.post(f"{self.url}/control/acquire", timeout=10)

            except requests.RequestException as error:
                raise GatewayError(f"Gateway {self.url} unreachable: {type(error).__name__}") from error

            if response.ok:
                break

            if response.status_code != 409 or "Retry-After" not in response.headers or time.monotonic() > deadline:
                raise GatewayError(f"Could not take control: {explain(response, 'robot.control')}")

            print(f"Waiting for control: {detail(response)}", flush=True)

            time.sleep(1.0)

        lease = response.json().get("lease") or {}

        previous = holder.get("client_id") if holder.get("active") else None

        if previous and previous != lease.get("client_id"):
            print(f"Took control from {previous} (it gets it back once this one releases)", flush=True)

        #
        # Renewed well inside the shortest lease in use (2 s in the
        # test gateways, 5 s by default).
        #
        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(0.5):
                try:
                    if self._session.post(f"{self.url}/control/renew", timeout=5).status_code != 200:
                        print("Lost control (preempted or expired)", file=sys.stderr, flush=True)
                        return

                except requests.RequestException as error:
                    print(f"Lease renewal failed: {type(error).__name__}", file=sys.stderr, flush=True)

        thread = threading.Thread(target=renew, daemon=True)

        thread.start()

        try:
            yield

        finally:
            stop.set()
            thread.join()

            try:
                self._session.post(f"{self.url}/control/release", timeout=10)

            except requests.RequestException:
                pass


def detail(
    response: requests.Response,
) -> str:
    try:
        value = response.json().get("detail", response.text)

    except ValueError:
        return response.text[:300]

    if isinstance(value, list):
        return "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg')}"
            for item in value
        )

    return str(value)


def explain(
    response: requests.Response,
    needs: str,
) -> str:
    """The gateway's answer, and what to do about a refused token."""

    text = f"HTTP {response.status_code}: {detail(response)}"

    if response.status_code == 401:
        return f"{text}\n  No or unknown token: give --token (or GATEWAY_TOKEN) of a client with {needs}"

    if response.status_code == 403:
        return f"{text}\n  This token's client lacks the permission: use a client with {needs}"

    return text


# ================================================================
# Arms and jobs
# ================================================================

def zyx_angles(
    pose: dict,
) -> tuple[float, float, float]:
    """(rx, ry, rz) in degrees of the pose's quaternion: the inverse
    of RAPID's OrientZYX(rz, ry, rx)."""

    w, x, y, z = (pose[name] for name in ("q1", "q2", "q3", "q4"))

    rx = math.atan2(2 * (y * z + w * x), 1 - 2 * (x * x + y * y))
    ry = math.asin(max(-1.0, min(1.0, 2 * (w * y - x * z))))
    rz = math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))

    return tuple(round(math.degrees(angle), 3) + 0.0 for angle in (rx, ry, rz))


def get_pose(
    gateway: Gateway,
    arm: str,
) -> dict:
    return gateway.get(f"/robot/arms/{arm}/pose", params={"tool": "tool0", "wobj": "wobj0"}).json()


def show_pose(
    pose: dict,
    arm: str,
    label: str,
) -> None:
    rx, ry, rz = zyx_angles(pose)

    print(
        f"{label}: {arm} arm at x={pose['x']:.1f} y={pose['y']:.1f} z={pose['z']:.1f} "
        f"rx={rx:.1f} ry={ry:.1f} rz={rz:.1f} "
        f"q=({pose['q1']:.3f}, {pose['q2']:.3f}, {pose['q3']:.3f}, {pose['q4']:.3f})"
    )


def run_job(
    gateway: Gateway,
    path: str,
    body: dict,
) -> dict:
    """Send one job (the lease held) and wait for it: with wait false
    by polling /robot/rws/status like a CommServer client."""

    print(f"POST {path} {body}", flush=True)

    result = gateway.post(path, json=body, timeout=JOB_TIMEOUT_SECONDS).json()

    print(f"-> {result['message']}", flush=True)

    if result["message"] == "STARTED":
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS

        while True:
            result = gateway.get("/robot/rws/status", params={"arm": body.get("arm", "left")}).json()

            if result["message"] != "BUSY" or time.monotonic() > deadline:
                break

            time.sleep(0.1)

        print(f"STATUS -> {result['message']}", flush=True)

    return result


def speed_fields(
    args: argparse.Namespace,
) -> dict:
    return {
        name: value
        for name, value in (
            ("speed", getattr(args, "speed", None)),
            ("acceleration", getattr(args, "acc", None)),
            ("ramp", getattr(args, "ramp", None)),
        )
        if value is not None
    }


def command_job(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    before = get_pose(gateway, args.arm)

    show_pose(before, args.arm, "Before")

    if args.command == "home":
        path, body = "/robot/rws/home", {}

    elif args.command in ("grip", "open"):
        path, body = "/robot/rws/gripper", {"action": args.command}

        if args.command == "grip" and args.force is not None:
            body["force"] = args.force

    elif args.command == "gripper-to":
        path, body = "/robot/rws/gripper", {"action": "move", "position": args.position}

    else:
        position = (args.x, args.y, args.z)
        rotation = (args.rx, args.ry, args.rz)

        if all(value is None for value in position + rotation):
            raise GatewayError("move: give --x/--y/--z and/or --rx/--ry/--rz")

        #
        # Whatever is not given stays as it is now.
        #
        path, body = "/robot/rws/move", {
            name: given if given is not None else now
            for name, given, now in zip("xyz", position, (before["x"], before["y"], before["z"]))
        }

        if any(value is not None for value in rotation):
            body.update(
                (name, given if given is not None else now)
                for name, given, now in zip(("rx", "ry", "rz"), rotation, zyx_angles(before))
            )

    body.update(arm=args.arm, wait=not args.no_wait, **speed_fields(args))

    with gateway.control():
        result = run_job(gateway, path, body)

    show_pose(get_pose(gateway, args.arm), args.arm, "After")

    return 0 if result["message"] == "DONE" else 1


def command_arms(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    for arm in ARMS:
        show_pose(get_pose(gateway, arm), arm, "Pose")

        status = gateway.get("/robot/rws/status", params={"arm": arm}).json()

        print(f"      {arm} arm job status: {status['message']}")

    return 0


def command_status(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    health = gateway.get("/health").json()

    print(
        f"Gateway {gateway.url}: {health.get('implementation', 'python')} {health['version']}, "
        f"{health['status']}, security {health['security_mode']}, up {health['uptime_seconds']:.0f} s"
    )

    camera = health["camera"]

    print(f"  camera  {'connected' if camera['connected'] else 'DISCONNECTED'}{'' if camera['connected'] else ': ' + str(camera.get('error'))}")

    robot = health["robot_controller"]

    if robot["reachable"]:
        print(
            f"  robot   {robot.get('operation_mode')}, motors {robot.get('controller_state')}, "
            f"RAPID {robot.get('rapid_execution_state')}"
        )

    else:
        print(f"  robot   unreachable: {robot.get('error')}")

    control = gateway.get("/control").json()

    print(f"  control {describe_lease(control)}")

    return 0 if health["status"] == "ok" else 1


def describe_lease(
    control: dict,
) -> str:
    if not control.get("active"):
        return "free"

    return f"held by {control.get('client_id')} (priority {control.get('priority')}), expires {control.get('expires_at')}"


# ================================================================
# RAPID program
# ================================================================

def rapid_status(
    gateway: Gateway,
) -> dict:
    return gateway.get("/robot/rapid").json()


def task_names(
    rapid: dict,
) -> list[str]:
    return [task["name"] for task in rapid["tasks"]]


def loaded_modules(
    rapid: dict,
    task: str,
) -> list[str]:
    return next((entry["modules"] for entry in rapid["tasks"] if entry["name"] == task), [])


def existing_tasks(
    rapid: dict,
    wanted: list[str],
) -> list[str]:
    names = task_names(rapid)

    for task in wanted:
        if task not in names:
            print(f"{task}: not on this controller, skipped")

    return [task for task in wanted if task in names]


def declared_module(
    source: str,
) -> str:
    match = re.search(r"^\s*MODULE\s+([A-Za-z][A-Za-z0-9_]*)", source, re.IGNORECASE | re.MULTILINE)

    if not match:
        raise GatewayError("The source declares no MODULE")

    return match.group(1)


def declared_values(
    module: Path,
) -> list[dict]:
    """The PERS declarations of a module with the values written."""

    return [
        {"name": match.group(1), "value": match.group(2)}
        for match in re.finditer(
            r"^\s*PERS\s+\w+\s+(\w+)\s*:=\s*(.*?);",
            module.read_text(encoding="iso-8859-1"),
            re.MULTILINE,
        )
    ]


def confirm(
    gateway: Gateway,
    action: str,
    assume_yes: bool,
) -> None:
    if assume_yes:
        return

    answer = input(f"{action} on the controller of {gateway.url}. Continue? [y/N] ")

    if answer.strip().lower() not in ("y", "yes"):
        raise GatewayError("Cancelled, nothing changed")


def backup(
    gateway: Gateway,
    tasks: list[str],
) -> Path:
    """Save every program module of the tasks to rapid/backups/."""

    host = urlsplit(gateway.url).hostname or "gateway"

    folder = BACKUP_DIR / f"{host}-{time.strftime('%Y%m%d-%H%M%S')}"

    rapid = rapid_status(gateway)

    for task in tasks:
        for module in loaded_modules(rapid, task):
            source = gateway.get(f"/robot/rapid/tasks/{task}/modules/{module}").json()["source"]

            path = folder / task / f"{module}.mod"

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="iso-8859-1", newline="")

            print(f"{task}: saved {module}")

    print(f"Backup: {folder}")

    return folder


def stop_rapid(
    gateway: Gateway,
) -> None:
    gateway.post("/robot/rapid/stop", timeout=RAPID_TIMEOUT_SECONDS)

    print("RAPID stopped")


def start_rapid(
    gateway: Gateway,
    reset_program_pointer: bool,
) -> None:
    rapid = gateway.post(
        "/robot/rapid/start",
        json={"reset_program_pointer": reset_program_pointer},
        timeout=RAPID_TIMEOUT_SECONDS,
    ).json()

    print(f"RAPID {rapid['execution_state']}" + (" (program pointer at main)" if reset_program_pointer else ""))


def unload(
    gateway: Gateway,
    task: str,
    module: str,
) -> None:
    path = f"/robot/rapid/tasks/{task}/modules/{module}"

    #
    # Right after a stop the controller sometimes still refuses an
    # unload ("current PGM state"); it accepts it moments later.
    #
    deadline = time.monotonic() + 5

    while True:
        try:
            gateway.request("DELETE", path, "program")

            break

        except GatewayError as error:
            if "PGM state" not in str(error) or time.monotonic() > deadline:
                raise

            time.sleep(0.5)

    print(f"{task}: unloaded {module}")


def load(
    gateway: Gateway,
    task: str,
    source: str,
    label: str,
) -> None:
    module = declared_module(source)

    gateway.request(
        "PUT",
        f"/robot/rapid/tasks/{task}/modules/{module}",
        "program",
        json={"source": source},
    )

    print(f"{task}: loaded {label}")


def replace_modules(
    gateway: Gateway,
    program: dict[str, list[Path]],
    start: bool,
) -> None:
    """Stop RAPID and give each task exactly these modules."""

    stop_rapid(gateway)

    rapid = rapid_status(gateway)

    for task, files in program.items():
        for module in loaded_modules(rapid, task):
            unload(gateway, task, module)

        for file in files:
            load(gateway, task, file.read_text(encoding="iso-8859-1"), file.name)

    if start:
        start_rapid(gateway, reset_program_pointer=True)

    else:
        print("Not started (start with: gatewayctl.py rapid start --main)")


def pending_jobs(
    gateway: Gateway,
) -> list[str]:
    """The arms whose job RAPID has not finished (move_request still
    set: /robot/rws/status answers BUSY, also while RAPID is stopped).
    An arm without the motion program (409) has none."""

    return [
        arm
        for arm in ARMS
        if gateway.get("/robot/rws/status", params={"arm": arm}, ok=(200, 409)).json().get("message") == "BUSY"
    ]


def has_smartgripper(
    gateway: Gateway,
) -> bool:
    """The real YuMi has it; the RobotStudio virtual controller not."""

    return gateway.get("/robot/arms/left/gripper", ok=(200, 404)).status_code == 200


def command_rapid(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    action = args.action

    if action == "status":
        rapid = rapid_status(gateway)

        print(
            f"Mode {rapid['operation_mode']}, motors {rapid['controller_state']}, "
            f"RAPID {rapid['execution_state']}"
        )

        for task in rapid["tasks"]:
            print(f"  {task['name']:<14} {task['execution_state']:<5} {', '.join(task['modules']) or '-'}")

        return 0

    if action == "backup":
        backup(gateway, existing_tasks(rapid_status(gateway), args.task or list(PROGRAM)))

        return 0

    if action == "stop":
        with gateway.control():
            stop_rapid(gateway)

        return 0

    if action == "start":
        with gateway.control():
            if rapid_status(gateway)["execution_state"] == "running":
                print("RAPID already running")

                return 0

            #
            # Continuing where RAPID stopped resumes the instruction it
            # stopped in: after a Stop or a collision mid-job, that is
            # the same motion again. From main the arm loop clears the
            # job instead.
            #
            pending = [] if args.main or args.resume else pending_jobs(gateway)

            if pending:
                raise GatewayError(
                    f"The {' and '.join(pending)} arm{'s have' if len(pending) > 1 else ' has'} an unfinished job "
                    f"(RAPID was stopped mid-job, by Stop or a collision): continuing would run that motion again. "
                    f"Use 'rapid start --main' (the job is dropped, the arm stays where it is), 'rapid restart' "
                    f"or 'rapid reset'; --resume continues the job anyway"
                )

            start_rapid(gateway, reset_program_pointer=args.main)

        return 0

    if action == "restart":
        with gateway.control():
            stop_rapid(gateway)
            start_rapid(gateway, reset_program_pointer=True)

        return 0

    if action == "reset":
        values = declared_values(SHARED_VARS)

        rapid = rapid_status(gateway)

        #
        # The variables are shared by the tasks that declare them,
        # so one task with SharedVars loaded is enough.
        #
        task = next(
            (task for task in task_names(rapid) if "SharedVars" in loaded_modules(rapid, task)),
            None,
        )

        if task is None:
            raise GatewayError("SharedVars is not loaded in any task (rapid upload first)")

        with gateway.control():
            rapid = gateway.post(
                "/robot/rapid/reset",
                needs="program",
                json={"task": task, "variables": values, "start": not args.no_start},
                timeout=RAPID_TIMEOUT_SECONDS,
            ).json()

        print(f"{len(values)} variables of SharedVars back to their declared values (in {task})")
        print(f"RAPID {rapid['execution_state']}")

        return 0

    if action == "upload":
        rapid = rapid_status(gateway)

        tasks = existing_tasks(rapid, args.task or list(PROGRAM))

        program = {task: [RAPID_DIR / name for name in PROGRAM[task]] for task in tasks}

        if args.gripper_stub:
            if has_smartgripper(gateway):
                raise GatewayError(
                    "--gripper-stub is for a virtual controller only; this one has a SmartGripper (the real robot?)"
                )

            for task in tasks:
                if task in ARM_TASKS:
                    program[task].append(GRIPPER_STUB)

        confirm(gateway, f"Replace the modules of {', '.join(tasks)}", args.yes)

        with gateway.control():
            backup(gateway, tasks)

            replace_modules(gateway, program, start=not args.no_start)

        return 0

    if action == "delete":
        rapid = rapid_status(gateway)

        loaded = loaded_modules(rapid, args.task)

        modules = args.modules or loaded

        missing = [module for module in modules if module.lower() not in {name.lower() for name in loaded}]

        if missing:
            raise GatewayError(f"Not loaded in {args.task}: {', '.join(missing)} (loaded: {', '.join(loaded) or '-'})")

        if not modules:
            print(f"{args.task} has no program modules")
            return 0

        confirm(gateway, f"Unload {', '.join(modules)} from {args.task} (RAPID is stopped and left stopped)", args.yes)

        with gateway.control():
            backup(gateway, [args.task])

            stop_rapid(gateway)

            for module in modules:
                unload(gateway, args.task, module)

        print("RAPID left stopped (a task without main cannot start; upload or restore first)")

        return 0

    if action == "restore":
        folder = args.folder

        if not folder.is_dir():
            raise GatewayError(f"{folder} is not a folder")

        program = {
            task.name: sorted(task.glob("*.mod"))
            for task in sorted(folder.iterdir())
            if task.is_dir()
        }

        if not program:
            raise GatewayError(f"{folder} has no <task>/*.mod folders")

        tasks = existing_tasks(rapid_status(gateway), list(program))

        program = {task: program[task] for task in tasks}

        confirm(gateway, f"Restore {', '.join(tasks)} from {folder}", args.yes)

        with gateway.control():
            backup(gateway, tasks)

            replace_modules(gateway, program, start=not args.no_start)

        return 0

    raise AssertionError(action)


def command_reset_home(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    """RAPID reset, both arms home, both grippers closed."""

    confirm(gateway, "Reset RAPID, then send both arms straight home and close the grippers", args.yes)

    command_rapid(gateway, argparse.Namespace(action="reset", no_start=False))

    with gateway.control():
        for arm in ARMS:
            if run_job(gateway, "/robot/rws/home", {"arm": arm, "speed": args.speed})["message"] != "DONE":
                raise GatewayError(f"Stopped: the {arm} arm did not get home")

        for arm in ARMS:
            if run_job(gateway, "/robot/rws/gripper", {"arm": arm, "action": "grip"})["message"] != "DONE":
                raise GatewayError(f"Stopped: the {arm} gripper did not close")

    print("Done: both arms home, both grippers closed")

    return 0


# ================================================================
# Camera
# ================================================================

def fetch_frames(
    gateway: Gateway,
    depth: bool,
):
    import numpy as np

    rgb_response = gateway.get("/camera/rgb", "camera")

    width = int(rgb_response.headers["X-Frame-Width"])
    height = int(rgb_response.headers["X-Frame-Height"])

    if rgb_response.headers["X-Pixel-Format"] != "BGR888p" or len(rgb_response.content) != 3 * width * height:
        raise GatewayError(f"Unexpected RGB frame: {rgb_response.headers['X-Pixel-Format']}, {len(rgb_response.content)} bytes")

    #
    # BGR888p is planar (all B, then G, then R); OpenCV wants BGR
    # interleaved.
    #
    bgr = np.ascontiguousarray(
        np.frombuffer(rgb_response.content, np.uint8).reshape(3, height, width).transpose(1, 2, 0)
    )

    if not depth:
        return bgr, None

    depth_response = gateway.get("/camera/depth", "camera")

    depth_image = np.frombuffer(depth_response.content, np.uint16).reshape(
        int(depth_response.headers["X-Frame-Height"]),
        int(depth_response.headers["X-Frame-Width"]),
    )

    return bgr, depth_image


def command_camera(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    import cv2
    import numpy as np

    if args.action == "rgb":
        bgr, _ = fetch_frames(gateway, depth=False)

        if not cv2.imwrite(str(args.output), bgr):
            raise GatewayError(f"Could not write {args.output}")

        print(f"Saved {bgr.shape[1]}x{bgr.shape[0]} image to {args.output.resolve()}")

        return 0

    #
    # capture: RGB, 16-bit depth (mm, 0 = unknown) and a colourised
    # depth for eyeballing; depth is aligned to RGB, so depth[v, u]
    # is the distance of rgb[v, u].
    #
    folders = {name: args.output / name for name in ("rgb", "depth", "depth_vis")}

    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)

    saved = 0

    try:
        while True:
            if saved:
                #
                # The gateway keeps only the newest frame: nothing
                # to drain in between.
                #
                time.sleep(args.interval)

            bgr, depth = fetch_frames(gateway, depth=True)

            stamp = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{saved:04d}"

            cv2.imwrite(str(folders["rgb"] / f"rgb_{stamp}.jpg"), bgr)
            cv2.imwrite(str(folders["depth"] / f"depth_{stamp}.png"), depth)

            valid = depth[depth > 0]

            if valid.size:
                norm = np.clip(depth.astype(np.float32) / valid.max(), 0, 1)
                vis = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
                vis[depth == 0] = 0
                cv2.imwrite(str(folders["depth_vis"] / f"vis_{stamp}.jpg"), vis)

            saved += 1

            centre = int(depth[depth.shape[0] // 2, depth.shape[1] // 2])

            print(f"Saved [{saved}] {stamp}  centre depth = {centre} mm", flush=True)

            if args.count and saved >= args.count:
                break

    except KeyboardInterrupt:
        pass

    return 0


# ================================================================
# Calibration, control, events
# ================================================================

def command_calibration(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    if args.action == "get":
        stored = gateway.get("/calibration", "camera").json()

        text = json.dumps(stored["calibration"], indent=2)

        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")

            print(f"Saved the calibration ({stored['updated_by']}, {stored['updated_at']}) to {args.output}")

        else:
            print(f"# updated by {stored['updated_by']} at {stored['updated_at']}", file=sys.stderr)
            print(text)

        return 0

    try:
        calibration = json.loads(args.file.read_text(encoding="utf-8"))

    except (OSError, ValueError) as error:
        raise GatewayError(f"{args.file} is not a readable JSON file: {error}") from error

    if not isinstance(calibration, dict):
        raise GatewayError(f"{args.file} holds no JSON object")

    with gateway.control():
        gateway.post("/calibration", json=calibration)

    print(f"Uploaded {args.file}")

    return 0


def command_control(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    if args.action == "show":
        print(describe_lease(gateway.get("/control").json()))

        return 0

    gateway.post("/control/release", needs="read")

    print("Released (if this client held the lease)")

    return 0


def command_events(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    from websockets.sync.client import connect

    url = re.sub(r"^http", "ws", gateway.url) + "/events"

    client = gateway.headers.get("X-Dev-Client")

    if client:
        url += f"?client={client}"

    token = gateway.headers.get("Authorization")

    received = 0

    with connect(url, additional_headers={"Authorization": token} if token else None) as websocket:
        websocket.send(json.dumps({"action": "subscribe", "events": args.events}))

        print(f"Subscribed to {', '.join(args.events)}", file=sys.stderr, flush=True)

        try:
            for message in websocket:
                event = json.loads(message)

                kind = str(event.get("type"))

                #
                # The gateway's replies to the subscription itself.
                #
                if kind == "error" or kind.startswith("subscription."):
                    print(json.dumps(event), file=sys.stderr, flush=True)

                    if kind in ("error", "subscription.rejected"):
                        return 1

                    continue

                print(json.dumps(event) if args.json else json.dumps(event, indent=2), flush=True)

                received += 1

                if args.count and received >= args.count:
                    break

        except KeyboardInterrupt:
            pass

    return 0


# ================================================================
# Smoke test
# ================================================================

def command_smoke(
    gateway: Gateway,
    args: argparse.Namespace,
) -> int:
    """Check a running gateway without moving the robot."""

    failed = 0

    def check(name: str, ok: bool, detail_text: str = "") -> bool:
        nonlocal failed

        print(f"{'ok  ' if ok else 'FAIL'}  {name}{f': {detail_text}' if detail_text else ''}", flush=True)

        failed += not ok

        return ok

    reader = Gateway(gateway.url, args.read_token, None, gateway._session.verify) if args.read_token else gateway

    any_status = tuple(range(100, 600))

    def get(path: str, client: Gateway = reader, **kwargs) -> requests.Response:
        return client.get(path, ok=any_status, timeout=10, **kwargs)

    health = get("/health")

    if not check("GET /health", health.status_code == 200, f"HTTP {health.status_code}"):
        return 1

    body = health.json()

    print(
        f"      {body.get('implementation', 'python')} {body['version']}, status {body['status']}, "
        f"security {body['security_mode']}, up {body['uptime_seconds']:.0f} s"
    )

    check("camera connected", body["camera"]["connected"], body["camera"].get("error") or "")

    robot = body["robot_controller"]

    print(f"      robot controller: {'reachable' if robot['reachable'] else robot['error']}")

    root = gateway.url.rsplit("/api/", 1)[0]

    openapi = requests.get(f"{root}/openapi.json", timeout=10, verify=gateway._session.verify)

    check("GET /openapi.json", openapi.status_code == 200 and "paths" in openapi.json(), f"HTTP {openapi.status_code}")

    if body["security_mode"] == "strict":
        refused = get("/camera/status", Gateway(gateway.url, None, None, gateway._session.verify))

        check("no token is refused", refused.status_code == 401, f"HTTP {refused.status_code}")

    status = get("/camera/status")

    check("GET /camera/status", status.status_code == 200, status.text[:200])

    intrinsics = get("/camera/intrinsics")

    if check("GET /camera/intrinsics", intrinsics.status_code == 200, f"HTTP {intrinsics.status_code}"):
        values = intrinsics.json()

        print(f"      {values['width']}x{values['height']}, fx {values['fx']:.1f}, fy {values['fy']:.1f}")

    rgb = get("/camera/rgb")

    if check("GET /camera/rgb", rgb.status_code == 200, f"HTTP {rgb.status_code}"):
        width = int(rgb.headers["X-Frame-Width"])
        height = int(rgb.headers["X-Frame-Height"])

        check(
            "RGB frame is BGR888p of its size",
            rgb.headers["X-Pixel-Format"] == "BGR888p" and len(rgb.content) == 3 * width * height,
            f"{width}x{height}, {len(rgb.content)} bytes, frame {rgb.headers.get('X-Frame-Sequence')}",
        )

    #
    # Stereo needs a few seconds after the camera starts.
    #
    deadline = time.monotonic() + args.depth_seconds
    valid = 0.0

    while True:
        depth = get("/camera/depth")

        if depth.status_code != 200:
            break

        width = int(depth.headers["X-Frame-Width"])
        height = int(depth.headers["X-Frame-Height"])

        values = array.array("H")
        values.frombytes(depth.content)

        sample = values[::97]
        valid = sum(1 for value in sample if value) / len(sample)

        if valid > 0 or time.monotonic() > deadline:
            break

        time.sleep(1)

    if check("GET /camera/depth", depth.status_code == 200, f"HTTP {depth.status_code}"):
        check(
            "depth frame is RAW16 of its size",
            depth.headers["X-Pixel-Format"] == "RAW16" and len(depth.content) == 2 * width * height,
            f"{width}x{height}, {len(depth.content)} bytes",
        )

        check("depth has valid pixels", valid > 0, f"{100 * valid:.0f} % of sampled pixels")

    robot_status = get("/robot/status")

    check("GET /robot/status", robot_status.status_code == 200, robot_status.text[:200])

    control = get("/control")

    check("GET /control", control.status_code == 200, control.text[:200])

    if not args.read_only:
        try:
            with gateway.control():
                check("control lease acquired and renewed", True)

                if args.write_calibration:
                    sample = {"smoke_test": time.time()}

                    stored = gateway.post("/calibration", json=sample, ok=any_status)

                    check("POST /calibration", stored.status_code == 200, stored.text[:200])

                    read_back = get("/calibration")

                    check(
                        "GET /calibration returns it",
                        read_back.status_code == 200 and read_back.json()["calibration"] == sample,
                        read_back.text[:200],
                    )

        except GatewayError as error:
            check("control lease", False, str(error))

        else:
            check("control lease released", not gateway.get("/control").json().get("active"))

    print(f"\n{'all checks passed' if failed == 0 else f'{failed} check(s) FAILED'}")

    return 1 if failed else 0


# ================================================================
# Tokens
# ================================================================

def command_token(
    gateway: Gateway | None,
    args: argparse.Namespace,
) -> int:
    token = secrets.token_urlsafe(32)

    entry = {
        args.client_id: {
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "permissions": args.permissions,
            "control_priority": args.priority,
        }
    }

    print("Token (give this to the client, it is not stored anywhere):")
    print(token)
    print()
    print("Entry for gateway/auth_clients.json (restart the gateway after adding it):")
    print(json.dumps(entry, indent=4))

    return 0


# ================================================================
# Main
# ================================================================

def add_arm_options(
    parser: argparse.ArgumentParser,
    speed: bool,
) -> None:
    parser.add_argument("--arm", choices=ARMS, default="left")
    parser.add_argument("--no-wait", action="store_true", help="Answer STARTED at once and poll /robot/rws/status")

    if speed:
        parser.add_argument("--speed", type=float, help="TCP speed in mm/s, 1..1500 (gateway default 50)")
        parser.add_argument("--acc", type=float, help="Acceleration in %% of the maximum, 20..100 (default 100)")
        parser.add_argument("--ramp", type=float, help="Acceleration ramp in %%, 10..100 (default 100)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gatewayctl.py",
        description="Operate the Robot Gateway: robot jobs, RAPID program, camera, calibration, lease, events",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Tokens:" + __doc__.split("Tokens:", 1)[1].split("Robot jobs move", 1)[0],
    )

    parser.add_argument("--target", choices=TARGETS, help="A known gateway (local, lynx, pi)")
    parser.add_argument("--url", help=f"Gateway API base URL (default: GATEWAY_URL, else {TARGETS['local']})")
    parser.add_argument("--token", default=os.environ.get("GATEWAY_TOKEN"), help="Bearer token (default: GATEWAY_TOKEN)")
    parser.add_argument("--client", help="Client id in development mode (X-Dev-Client)")
    parser.add_argument("--ca", help="CA certificate file for an https gateway")
    parser.add_argument("-y", "--yes", action="store_true", help="Do not ask before changing the RAPID program")

    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    commands.add_parser("status", help="Gateway health, camera, robot and the lease (exit 1 if degraded)")

    commands.add_parser("arms", help="Both arms' poses and job status")

    move = commands.add_parser("move", help="Move one arm (values left out stay as they are)")

    for name in ("x", "y", "z"):
        move.add_argument(f"--{name}", type=float, help=f"{name} in mm (tool0 in wobj0)")

    for name in ("rx", "ry", "rz"):
        move.add_argument(f"--{name}", type=float, help=f"{name} in degrees (OrientZYX)")

    add_arm_options(move, speed=True)

    add_arm_options(commands.add_parser("home", help="Move one arm home"), speed=True)

    grip = commands.add_parser("grip", help="Close one gripper")
    grip.add_argument("--force", type=float, help="Holding force in N, 0 < N <= 20 (default 20)")
    add_arm_options(grip, speed=False)

    add_arm_options(commands.add_parser("open", help="Open one gripper fully"), speed=False)

    gripper_to = commands.add_parser("gripper-to", help="Open or close one gripper to MM between the fingers")
    gripper_to.add_argument("position", type=float, metavar="MM", help="0 closed .. 25 fully open")
    add_arm_options(gripper_to, speed=False)

    reset_home = commands.add_parser("reset-home", help="RAPID reset, both arms home, both grippers closed (admin)")
    reset_home.add_argument("--speed", type=float, default=200, help="Home speed in mm/s (default 200)")

    rapid = commands.add_parser("rapid", help="The RAPID program (status, start, stop, upload, reset, ...)")
    actions = rapid.add_subparsers(dest="action", required=True, metavar="ACTION")

    actions.add_parser("status", help="Mode, motors, RAPID state and the modules of each task")

    start = actions.add_parser("start", help="Start RAPID (AUTO only); refused while an arm has an unfinished job")
    resume = start.add_mutually_exclusive_group()
    resume.add_argument("--main", action="store_true", help="Move the program pointer to main first (drops an unfinished job)")
    resume.add_argument("--resume", action="store_true", help="Continue where RAPID stopped, also an unfinished job (the same motion again)")

    actions.add_parser("stop", help="Stop RAPID (halts any move at once)")

    actions.add_parser("restart", help="Stop, program pointer to main, start")

    reset = actions.add_parser("reset", help="SharedVars' PERS back to their declared values, restart (admin)")
    reset.add_argument("--no-start", action="store_true", help="Leave RAPID stopped")

    upload = actions.add_parser("upload", help="Back up, stop, load the modules in rapid/, start (admin)")
    upload.add_argument("--task", action="append", choices=list(PROGRAM), help="Only this task (repeatable)")
    upload.add_argument("--no-start", action="store_true", help="Leave RAPID stopped")
    upload.add_argument("--gripper-stub", action="store_true", help="Also load sim/GripperStub.mod (virtual controller ONLY)")

    delete = actions.add_parser("delete", help="Back up, stop and unload modules of one task (admin)")
    delete.add_argument("--task", required=True, help="Task, e.g. T_ROB_L")
    delete.add_argument("modules", nargs="*", help="Module names (default: all program modules)")

    backup_parser = actions.add_parser("backup", help="Save the loaded modules to rapid/backups/")
    backup_parser.add_argument("--task", action="append", help="Only this task (repeatable)")

    restore = actions.add_parser("restore", help="Back up, then load a backup folder's modules (admin)")
    restore.add_argument("folder", type=Path, help="A folder made by backup/upload/delete")
    restore.add_argument("--no-start", action="store_true", help="Leave RAPID stopped")

    camera = commands.add_parser("camera", help="Save camera frames")
    camera_actions = camera.add_subparsers(dest="action", required=True, metavar="ACTION")

    rgb = camera_actions.add_parser("rgb", help="The newest RGB frame as an image file")
    rgb.add_argument("-o", "--output", type=Path, default=Path("rgb.png"), help="Image file (default rgb.png)")

    capture = camera_actions.add_parser("capture", help="RGB + 16-bit depth + depth visualisation, repeatedly")
    capture.add_argument("-o", "--output", type=Path, default=Path("out"), help="Folder (default out/)")
    capture.add_argument("--interval", type=float, default=10.0, help="Seconds between captures")
    capture.add_argument("--count", type=int, default=0, help="Stop after N captures (0 = until Ctrl+C)")

    calibration = commands.add_parser("calibration", help="Read or upload the stored calibration")
    calibration_actions = calibration.add_subparsers(dest="action", required=True, metavar="ACTION")

    get = calibration_actions.add_parser("get", help="Print it, or save it with -o")
    get.add_argument("-o", "--output", type=Path, help="JSON file to write")

    put = calibration_actions.add_parser("put", help="Upload a JSON object (replaces the stored one)")
    put.add_argument("file", type=Path)

    control = commands.add_parser("control", help="The control lease")
    control_actions = control.add_subparsers(dest="action", required=True, metavar="ACTION")
    control_actions.add_parser("show", help="Who holds it")
    control_actions.add_parser("release", help="Release this client's lease")

    events = commands.add_parser("events", help="Print gateway events (Ctrl+C to stop)")
    events.add_argument("--events", nargs="+", default=EVENTS, help="Event names or prefixes (default: all)")
    events.add_argument("--count", type=int, default=0, help="Stop after N events")
    events.add_argument("--json", action="store_true", help="One JSON object per line")

    smoke = commands.add_parser("smoke", help="Check the gateway without moving the robot (exit 0 if all pass)")
    smoke.add_argument("--read-token", help="Token for the read checks (default: --token)")
    smoke.add_argument("--read-only", action="store_true", help="Skip the control lease")
    smoke.add_argument("--write-calibration", action="store_true", help="Also replace the stored calibration with a test object")
    smoke.add_argument("--depth-seconds", type=float, default=15, help="How long to wait for valid depth")

    token = commands.add_parser("token", help="Client tokens")
    token_actions = token.add_subparsers(dest="action", required=True, metavar="ACTION")

    new = token_actions.add_parser("new", help="A random token and its auth_clients.json entry")
    new.add_argument("client_id", help="Key of the client in auth_clients.json")
    new.add_argument("--priority", type=int, default=50, help="Control priority (higher preempts)")
    new.add_argument(
        "--permissions",
        nargs="+",
        default=["robot.read", "robot.control", "camera.read"],
        help="Permissions to grant",
    )

    #
    # -y also after the subcommand (rapid upload -y).
    #
    for changing in (upload, delete, restore, reset_home):
        changing.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="Do not ask first")

    return parser


HANDLERS = {
    "status": command_status,
    "arms": command_arms,
    "move": command_job,
    "home": command_job,
    "grip": command_job,
    "open": command_job,
    "gripper-to": command_job,
    "reset-home": command_reset_home,
    "rapid": command_rapid,
    "camera": command_camera,
    "calibration": command_calibration,
    "control": command_control,
    "events": command_events,
    "smoke": command_smoke,
    "token": command_token,
}


def main(
    argv: list[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)

    url = args.url or (TARGETS[args.target] if args.target else os.environ.get("GATEWAY_URL", TARGETS["local"]))

    gateway = Gateway(url, args.token, args.client, args.ca or True)

    try:
        return HANDLERS[args.command](gateway, args)

    except GatewayError as error:
        print(f"Error: {error}", file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
