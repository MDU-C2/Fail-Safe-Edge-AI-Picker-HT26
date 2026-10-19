"""
Manage the RAPID program on the YuMi over Robot Web Services (RWS 1.0,
RobotWare 6): status, start, stop, restart, upload, delete, backup,
restore. Defaults to the REAL robot at 192.168.125.1; use
--host 127.0.0.1 for the RobotStudio virtual controller.

    python rapid/rapidctl.py status
    python rapid/rapidctl.py upload                    # back up, stop, load the files below, start
    python rapid/rapidctl.py upload --task T_ROB_L     # only one task
    python rapid/rapidctl.py upload --no-start
    python rapid/rapidctl.py start                     # continue where RAPID stopped
    python rapid/rapidctl.py start --main              # program pointer to main first
    python rapid/rapidctl.py stop
    python rapid/rapidctl.py restart                   # stop, pointer to main, start
    python rapid/rapidctl.py delete --task T_ROB_L     # unload every module of a task
    python rapid/rapidctl.py delete --task T_ROB_L LeftArm
    python rapid/rapidctl.py backup
    python rapid/rapidctl.py restore rapid/backups/<dir>
    python rapid/rapidctl.py --host 127.0.0.1 upload --gripper-stub

The program (same modules as the .pgf files):
    T_ROB_L        SharedVars + LeftArmInstructions + GoHomeLeft
    T_ROB_R        SharedVars + RightArmInstructions + GoHomeRight
    communication  SharedVars + CommServer   (only on the real YuMi)

Notes:
    - Changing the program (upload, delete, restore) needs the robot in
      AUTO; in manual mode the controller refuses remote changes, and
      RAPID can only be started from the FlexPendant there.
    - upload, delete and restore first save the modules they replace
      to rapid/backups/<host>-<time>/ (git-ignored); restore loads such
      a folder back.
    - They ask before changing a controller that is not 127.0.0.1
      (-y skips the question).
    - Starting does not move the arms: they wait for a job (the
      grippers may calibrate once). Stopping halts any move at once.
    - --gripper-stub also loads sim/GripperStub.mod into the arm tasks:
      ONLY for a virtual controller without the SmartGripper option.
      Never on the real robot (it refuses with --gripper-stub there).
    - Each module is uploaded to $HOME/<file name> on the controller
      and loaded from there.

Needs: requests
"""

import argparse
import html
import re
import sys
import time
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth


RAPID_DIR = Path(__file__).resolve().parent

BACKUP_DIR = RAPID_DIR / "backups"

GRIPPER_STUB = RAPID_DIR / "sim" / "GripperStub.mod"

PROGRAM = {
    "T_ROB_L": ["SharedVars.mod", "LeftArmInstructions.mod", "GoHomeLeft.mod"],
    "T_ROB_R": ["SharedVars.mod", "RightArmInstructions.mod", "GoHomeRight.mod"],
    "communication": ["SharedVars.mod", "CommServer.mod"],
}

ARM_TASKS = ("T_ROB_L", "T_ROB_R")

REAL_ROBOT = "192.168.125.1"

START_ARGUMENTS = {
    "regain": "continue",
    "execmode": "continue",
    "cycle": "forever",
    "condition": "none",
    "stopatbp": "disabled",
    "alltaskbytsp": "false",
}


# ================================================================
# Controller
# ================================================================

class ControllerError(Exception):
    pass


class Controller:
    def __init__(self, host: str, user: str, password: str) -> None:
        self.host = host
        self._base = f"http://{host}"
        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth(user, password)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self._session.request(method, f"{self._base}{path}", timeout=30, **kwargs)
        except requests.RequestException as error:
            raise ControllerError(f"Controller {self.host} unreachable: {type(error).__name__}") from error

        if response.status_code >= 400:
            #
            # Errors come as XHTML; the message is in <span class="msg">.
            #
            message = re.search(r'<span class="msg">(.*?)</span>', response.text, re.DOTALL)
            text = html.unescape(message.group(1)) if message else response.text[:300]

            raise ControllerError(f"{method} {path} failed with {response.status_code}: {text}")

        return response

    def post(self, path: str, data: dict | None = None) -> None:
        #
        # RWS rejects a POST without this content type, even when the
        # body is empty.
        #
        self._request(
            "POST",
            path,
            data=data or {},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def get_state(self, path: str) -> list[dict]:
        separator = "&" if "?" in path else "?"

        return self._request("GET", f"{path}{separator}json=1").json()["_embedded"]["_state"]

    # ---------------------------------------------------------------- state

    def system(self) -> dict:
        return self.get_state("/rw/system")[0]

    def execution_state(self) -> str:
        return self.get_state("/rw/rapid/execution")[0]["ctrlexecstate"]

    def operation_mode(self) -> str:
        return self.get_state("/rw/panel/opmode")[0]["opmode"]

    def motors(self) -> str:
        return self.get_state("/rw/panel/ctrlstate")[0]["ctrlstate"]

    def tasks(self) -> list[dict]:
        return self.get_state("/rw/rapid/tasks")

    def program_modules(self, task: str) -> list[str]:
        return [
            module["name"]
            for module in self.get_state(f"/rw/rapid/modules?task={task}")
            if module.get("type") == "ProgMod"
        ]

    # ---------------------------------------------------------------- execution

    def stop(self) -> None:
        self.post("/rw/rapid/execution?action=stop", {"stopmode": "stop"})

        #
        # The controller reports stopped a moment before the normal
        # tasks have; until then it refuses to reset the program
        # pointer. Background (semi-static) tasks keep running.
        #
        deadline = time.monotonic() + 10

        while (
            self.execution_state() != "stopped"
            or any(task["excstate"] == "star" for task in self.tasks() if task.get("type") == "norm")
        ):
            if time.monotonic() > deadline:
                raise ControllerError("RAPID did not stop within 10 s")

            time.sleep(0.2)

    def reset_program_pointer(self) -> None:
        deadline = time.monotonic() + 5

        while True:
            try:
                with self.mastership():
                    self.post("/rw/rapid/execution?action=resetpp")

                return

            except ControllerError:
                if time.monotonic() > deadline:
                    raise

                time.sleep(0.5)

    def start(self) -> None:
        self.post("/rw/rapid/execution?action=start", START_ARGUMENTS)

        #
        # A program with errors (e.g. a missing module) is accepted
        # here but does not run.
        #
        deadline = time.monotonic() + 3

        while self.execution_state() != "running":
            if time.monotonic() > deadline:
                raise ControllerError(
                    "RAPID did not start; the program probably has errors "
                    "(see the FlexPendant event log), or the program pointer "
                    "is lost (try: start --main)"
                )

            time.sleep(0.2)

    # ---------------------------------------------------------------- modules

    class _Mastership:
        def __init__(self, controller: "Controller") -> None:
            self._controller = controller

        def __enter__(self) -> None:
            self._controller.post("/rw/mastership?action=request")

        def __exit__(self, *exc) -> None:
            self._controller.post("/rw/mastership?action=release")

    def mastership(self) -> "_Mastership":
        return self._Mastership(self)

    def save_module(self, task: str, module: str) -> str:
        """Return the source of a loaded module."""

        name = f"rapidctl_backup_{task}_{module}"

        self.post(f"/rw/rapid/modules/{module}?task={task}&action=save", {"name": name, "path": "$TEMP"})

        return self._request("GET", f"/fileservice/$TEMP/{name}.mod").content.decode("iso-8859-1")

    def unload(self, task: str, module: str) -> None:
        self.post(f"/rw/rapid/tasks/{task}?action=unloadmod", {"module": module})

    def load(self, task: str, file: Path) -> None:
        path = f"$HOME/{file.name}"

        self._request(
            "PUT",
            f"/fileservice/{path}",
            data=file.read_text(encoding="iso-8859-1").encode("iso-8859-1"),
            headers={"Content-Type": "text/plain;v=2.0"},
        )

        self.post(f"/rw/rapid/tasks/{task}?action=loadmod", {"modulepath": path, "replace": "true"})


# ================================================================
# Steps
# ================================================================

def existing_tasks(controller: Controller, wanted: list[str]) -> list[str]:
    names = {task["name"] for task in controller.tasks()}

    for task in wanted:
        if task not in names:
            print(f"{task}: not on this controller, skipped")

    return [task for task in wanted if task in names]


def confirm(controller: Controller, action: str, assume_yes: bool) -> None:
    if assume_yes or controller.host in ("127.0.0.1", "localhost"):
        return

    answer = input(f"{action} on {controller.host} ({controller.system()['name']}). Continue? [y/N] ")

    if answer.strip().lower() not in ("y", "yes"):
        sys.exit("Cancelled, nothing changed")


def check_auto(controller: Controller) -> None:
    mode = controller.operation_mode()

    if mode != "AUTO":
        sys.exit(
            f"The controller is in {mode}, not AUTO: it refuses remote program changes and "
            f"starts. Switch to AUTO on the FlexPendant (QuickSet menu, YuMi icon) first."
        )


def backup(controller: Controller, tasks: list[str]) -> Path:
    folder = BACKUP_DIR / f"{controller.host}-{time.strftime('%Y%m%d-%H%M%S')}"

    for task in tasks:
        for module in controller.program_modules(task):
            path = folder / task / f"{module}.mod"

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(controller.save_module(task, module), encoding="iso-8859-1")

            print(f"{task}: saved {module}")

    print(f"Backup: {folder}")

    return folder


def replace_modules(controller: Controller, program: dict[str, list[Path]], start: bool) -> None:
    """Stop RAPID and give each task exactly these modules."""

    was = controller.execution_state()

    controller.stop()

    if was != "stopped":
        print("RAPID stopped")

    with controller.mastership():
        for task, files in program.items():
            for module in controller.program_modules(task):
                controller.unload(task, module)

                print(f"{task}: unloaded {module}")

            for file in files:
                controller.load(task, file)

                print(f"{task}: loaded {file.name}")

    finish(controller, start)


def finish(controller: Controller, start: bool) -> None:
    controller.reset_program_pointer()

    print("Program pointer at main")

    if not start:
        print("Not started (start with: rapidctl.py start)")
        return

    controller.start()

    print(f"RAPID {controller.execution_state()}")


# ================================================================
# Commands
# ================================================================

def command_status(controller: Controller, args: argparse.Namespace) -> None:
    system = controller.system()

    print(f"Controller {controller.host}: {system['name']}, RobotWare {system['rwversionname']}")
    print(f"Mode {controller.operation_mode()}, motors {controller.motors()}, RAPID {controller.execution_state()}")

    for task in controller.tasks():
        modules = controller.program_modules(task["name"])

        print(f"  {task['name']:<14} {task['excstate']:<5} {', '.join(modules) or '-'}")


def command_start(controller: Controller, args: argparse.Namespace) -> None:
    check_auto(controller)

    if controller.execution_state() == "running":
        print("RAPID already running")
        return

    if args.main:
        controller.reset_program_pointer()

        print("Program pointer at main")

    controller.start()

    print(f"RAPID {controller.execution_state()}")


def command_stop(controller: Controller, args: argparse.Namespace) -> None:
    controller.stop()

    print("RAPID stopped")


def command_restart(controller: Controller, args: argparse.Namespace) -> None:
    check_auto(controller)

    controller.stop()

    print("RAPID stopped")

    finish(controller, start=True)


def command_upload(controller: Controller, args: argparse.Namespace) -> None:
    check_auto(controller)

    tasks = existing_tasks(controller, args.task or list(PROGRAM))

    program = {
        task: [RAPID_DIR / name for name in PROGRAM[task]]
        for task in tasks
    }

    if args.gripper_stub:
        for task in tasks:
            if task in ARM_TASKS:
                program[task].append(GRIPPER_STUB)

    confirm(controller, f"Replace the modules of {', '.join(tasks)}", args.yes)

    backup(controller, tasks)

    replace_modules(controller, program, start=not args.no_start)


def command_delete(controller: Controller, args: argparse.Namespace) -> None:
    check_auto(controller)

    task = args.task

    loaded = controller.program_modules(task)

    modules = args.modules or loaded

    missing = [module for module in modules if module not in loaded]

    if missing:
        sys.exit(f"Not loaded in {task}: {', '.join(missing)} (loaded: {', '.join(loaded) or '-'})")

    if not modules:
        print(f"{task} has no program modules")
        return

    confirm(controller, f"Unload {', '.join(modules)} from {task} (RAPID is stopped and left stopped)", args.yes)

    backup(controller, [task])

    controller.stop()

    with controller.mastership():
        for module in modules:
            controller.unload(task, module)

            print(f"{task}: unloaded {module}")

    print("RAPID left stopped (a task without main cannot start; upload or restore first)")


def command_backup(controller: Controller, args: argparse.Namespace) -> None:
    backup(controller, existing_tasks(controller, args.task or list(PROGRAM)))


def command_restore(controller: Controller, args: argparse.Namespace) -> None:
    check_auto(controller)

    folder = args.folder

    program = {
        task.name: sorted(task.glob("*.mod"))
        for task in sorted(folder.iterdir())
        if task.is_dir()
    }

    if not program:
        sys.exit(f"{folder} has no <task>/*.mod folders")

    tasks = existing_tasks(controller, list(program))

    program = {task: program[task] for task in tasks}

    confirm(controller, f"Restore {', '.join(tasks)} from {folder}", args.yes)

    backup(controller, tasks)

    replace_modules(controller, program, start=not args.no_start)


# ================================================================
# Main
# ================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage the RAPID program on the YuMi over Robot Web Services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Defaults to the REAL robot. Use --host 127.0.0.1 for RobotStudio.",
    )

    parser.add_argument("--host", default=REAL_ROBOT, help=f"Controller address (default: the real YuMi, {REAL_ROBOT})")
    parser.add_argument("--user", default="Default User")
    parser.add_argument("--password", default="robotics")
    parser.add_argument("-y", "--yes", action="store_true", help="Do not ask before changing the program")

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="Mode, motors, RAPID state and the modules of each task")

    start = commands.add_parser("start", help="Start RAPID (AUTO only)")
    start.add_argument("--main", action="store_true", help="Move the program pointer to main first")

    commands.add_parser("stop", help="Stop RAPID")

    commands.add_parser("restart", help="Stop, program pointer to main, start")

    upload = commands.add_parser("upload", help="Back up, stop, replace the modules with the files in rapid/, start")
    upload.add_argument("--task", action="append", choices=list(PROGRAM), help="Only this task (repeatable; default: all)")
    upload.add_argument("--no-start", action="store_true", help="Leave RAPID stopped")
    upload.add_argument("--gripper-stub", action="store_true", help="Also load sim/GripperStub.mod (virtual controller ONLY)")

    delete = commands.add_parser("delete", help="Back up, stop and unload modules of one task")
    delete.add_argument("--task", required=True, help="Task, e.g. T_ROB_L")
    delete.add_argument("modules", nargs="*", help="Module names as loaded (default: all program modules)")

    backup_parser = commands.add_parser("backup", help="Save the loaded modules to rapid/backups/")
    backup_parser.add_argument("--task", action="append", choices=list(PROGRAM), help="Only this task (repeatable)")

    restore = commands.add_parser("restore", help="Back up, then load a backup folder's modules")
    restore.add_argument("folder", type=Path, help="A folder made by backup/upload/delete")
    restore.add_argument("--no-start", action="store_true", help="Leave RAPID stopped")

    #
    # -y also after the command (upload -y); SUPPRESS keeps a -y given
    # before the command.
    #
    for changing in (upload, delete, restore):
        changing.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS, help="Do not ask first")

    args = parser.parse_args()

    if getattr(args, "gripper_stub", False) and args.host == REAL_ROBOT:
        parser.error("--gripper-stub is for a virtual controller only, never the real robot")

    controller = Controller(args.host, args.user, args.password)

    handlers = {
        "status": command_status,
        "start": command_start,
        "stop": command_stop,
        "restart": command_restart,
        "upload": command_upload,
        "delete": command_delete,
        "backup": command_backup,
        "restore": command_restore,
    }

    try:
        handlers[args.command](controller, args)

    except ControllerError as error:
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    main()
