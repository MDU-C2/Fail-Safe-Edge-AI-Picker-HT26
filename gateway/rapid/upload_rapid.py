"""
Upload the RAPID program to a YuMi controller over Robot Web Services
(RWS 1.0, RobotWare 6 / IRC5) and start it.

The .mod files here are the team's arm modules from rapid/, copied
unchanged, without the CommServer socket server: the gateway
commands them over RWS by writing the PERS variables in SharedVars
(POST /robot/rws/move, /home and /gripper).

What it does:
    1. Saves every program module in the two arm tasks to
       gateway/rapid/backup/<host>-<time>/ (git-ignored).
    2. Stops RAPID and takes mastership.
    3. Unloads every program module in the two arm tasks.
    4. Uploads the .mod files below to $HOME and loads them:
           T_ROB_L  SharedVars + LeftArmInstructions + GoHomeLeft
           T_ROB_R  SharedVars + RightArmInstructions + GoHomeRight
    5. Resets the program pointer, releases mastership and starts.

Other tasks (e.g. communication with the team's CommServer on the
real YuMi) are left as they are. A virtual controller without the
SmartGripper option lacks the gripper routines the arm modules call;
--gripper-stub also loads sim/GripperStub.mod into both tasks there. Defaults target the RobotStudio
virtual controller on this PC; pointing it at a real controller
replaces the program of both arms.

Usage (from the repository root):
    python gateway/rapid/upload_rapid.py
    python gateway/rapid/upload_rapid.py --gripper-stub
    python gateway/rapid/upload_rapid.py --no-start
    python gateway/rapid/upload_rapid.py --host 192.168.125.1 --restore gateway/rapid/backup/<dir>

Needs: requests
"""

import argparse
import html
import re
import time
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth


RAPID_DIR = Path(__file__).parent

BACKUP_DIR = RAPID_DIR / "backup"

GRIPPER_STUB = RAPID_DIR / "sim" / "GripperStub.mod"

PROGRAM = {
    "T_ROB_L": [
        "SharedVars.mod",
        "LeftArmInstructions.mod",
        "GoHomeLeft.mod",
    ],
    "T_ROB_R": [
        "SharedVars.mod",
        "RightArmInstructions.mod",
        "GoHomeRight.mod",
    ],
}


class Controller:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
    ) -> None:
        self._base = f"http://{host}"

        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth(user, password)

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> requests.Response:
        response = self._session.request(
            method,
            f"{self._base}{path}",
            timeout=30,
            **kwargs,
        )

        if response.status_code >= 400:
            #
            # Errors come as XHTML; the message is in <span class="msg">.
            #
            message = re.search(r'<span class="msg">(.*?)</span>', response.text, re.DOTALL)

            raise RuntimeError(
                f"{method} {path} failed with {response.status_code}: "
                f"{html.unescape(message.group(1)) if message else response.text[:300]}"
            )

        return response

    def post(
        self,
        path: str,
        data: dict | None = None,
    ) -> None:
        #
        # RWS rejects a POST without this content type, even when
        # the body is empty.
        #
        self._request(
            "POST",
            path,
            data=data or {},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

    def get_json(
        self,
        path: str,
    ) -> dict:
        separator = "&" if "?" in path else "?"

        return self._request(
            "GET",
            f"{path}{separator}json=1",
        ).json()

    def upload(
        self,
        name: str,
        text: str,
    ) -> str:
        path = f"$HOME/{name}"

        self._request(
            "PUT",
            f"/fileservice/{path}",
            data=text.encode("iso-8859-1"),
            headers={
                "Content-Type": "text/plain;v=2.0",
            },
        )

        return path

    def save_module(
        self,
        task: str,
        module: str,
    ) -> str:
        """Return the source of a loaded module."""

        name = f"upload_rapid_backup_{task}_{module}"

        self._request(
            "POST",
            f"/rw/rapid/modules/{module}?task={task}&action=save",
            data={"name": name, "path": "$TEMP"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        return self._request(
            "GET",
            f"/fileservice/$TEMP/{name}.mod",
        ).content.decode("iso-8859-1")

    def program_modules(
        self,
        task: str,
    ) -> list[str]:
        data = self.get_json(f"/rw/rapid/modules?task={task}")

        return [
            module["name"]
            for module in data["_embedded"]["_state"]
            if module.get("type") == "ProgMod"
        ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload and start the RAPID program over RWS",
    )

    parser.add_argument("--host", default="127.0.0.1", help="Controller address (default: local virtual controller)")
    parser.add_argument("--user", default="Default User")
    parser.add_argument("--password", default="robotics")
    parser.add_argument("--no-start", action="store_true", help="Load the program but do not start it")
    parser.add_argument("--restore", type=Path, metavar="BACKUP_DIR", help="Load the modules of a backup made by this script instead")
    parser.add_argument("--gripper-stub", action="store_true", help="Also load sim/GripperStub.mod (virtual controller without SmartGripper only)")

    args = parser.parse_args()

    program = {
        task: [RAPID_DIR / name for name in names]
        for task, names in PROGRAM.items()
    }

    if args.restore:
        program = {
            task: sorted((args.restore / task).glob("*.mod"))
            for task in PROGRAM
        }

        if not all(program.values()):
            parser.error(f"{args.restore} has no T_ROB_L/*.mod and T_ROB_R/*.mod")

    elif args.gripper_stub:
        for files in program.values():
            files.append(GRIPPER_STUB)

    controller = Controller(args.host, args.user, args.password)

    system = controller.get_json("/rw/system")["_embedded"]["_state"][0]

    print(f"Controller: {system['name']}, RobotWare {system['rwversionname']}")

    backup = BACKUP_DIR / f"{args.host}-{time.strftime('%Y%m%d-%H%M%S')}"

    for task in PROGRAM:
        for module in controller.program_modules(task):
            path = backup / task / f"{module}.mod"

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(controller.save_module(task, module), encoding="iso-8859-1")

            print(f"{task}: saved {module} to {path}")

    controller.post("/rw/rapid/execution?action=stop", {"stopmode": "stop"})

    #
    # Stopping takes a moment; until it has, the controller refuses
    # to unload modules (403).
    #
    deadline = time.monotonic() + 10

    while controller.get_json("/rw/rapid/execution")["_embedded"]["_state"][0]["ctrlexecstate"] != "stopped":
        if time.monotonic() > deadline:
            raise RuntimeError("RAPID did not stop within 10 s")

        time.sleep(0.2)

    controller.post("/rw/mastership?action=request")

    try:
        for task, files in program.items():
            for module in controller.program_modules(task):
                controller.post(f"/rw/rapid/tasks/{task}?action=unloadmod", {"module": module})

                print(f"{task}: unloaded {module}")

            for file in files:
                path = controller.upload(file.name, file.read_text(encoding="iso-8859-1"))

                controller.post(f"/rw/rapid/tasks/{task}?action=loadmod", {"modulepath": path, "replace": "true"})

                print(f"{task}: loaded {file.name}")

        controller.post("/rw/rapid/execution?action=resetpp")

        print("Program pointer reset")

    finally:
        controller.post("/rw/mastership?action=release")

    if args.no_start:
        print("Loaded, not started")
        return

    controller.post(
        "/rw/rapid/execution?action=start",
        {
            "regain": "continue",
            "execmode": "continue",
            "cycle": "forever",
            "condition": "none",
            "stopatbp": "disabled",
            "alltaskbytsp": "false",
        },
    )

    state = controller.get_json("/rw/rapid/execution")["_embedded"]["_state"][0]

    print(f"Started, execution state: {state['ctrlexecstate']}")


if __name__ == "__main__":
    main()
