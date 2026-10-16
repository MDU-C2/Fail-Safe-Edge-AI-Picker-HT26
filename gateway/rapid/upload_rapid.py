"""
Upload the RAPID program to a YuMi controller over Robot Web Services
(RWS 1.0, RobotWare 6 / IRC5) and start it.

The gateway commands the program over RWS by writing the PERS
variables in LegacyShared (POST /robot/rws/move and /robot/rws/home);
there is no socket server on the controller.

What it does:
    1. Stops RAPID and takes mastership.
    2. Unloads every program module in the two arm tasks.
    3. Uploads the .mod files below to $HOME and loads them:
           T_ROB_L  LegacyShared + LegacyMotion  (moves the left arm)
           T_ROB_R  LegacyShared + RightArmIdle  (placeholder main)
    4. Resets the program pointer, releases mastership and starts.

Defaults target the RobotStudio virtual controller on this PC. Pointing
it at a real controller replaces its whole RAPID program.

Usage (from the repository root):
    python gateway/rapid/upload_rapid.py
    python gateway/rapid/upload_rapid.py --no-start

Needs: requests
"""

import argparse
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth


RAPID_DIR = Path(__file__).parent

PROGRAM = {
    "T_ROB_L": [
        "LegacyShared.mod",
        "LegacyMotion.mod",
    ],
    "T_ROB_R": [
        "LegacyShared.mod",
        "RightArmIdle.mod",
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
            raise RuntimeError(
                f"{method} {path} failed with "
                f"{response.status_code}: {response.text[:300]}"
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

    args = parser.parse_args()

    controller = Controller(args.host, args.user, args.password)

    system = controller.get_json("/rw/system")["_embedded"]["_state"][0]

    print(f"Controller: {system['name']}, RobotWare {system['rwversionname']}")

    controller.post("/rw/rapid/execution?action=stop", {"stopmode": "stop"})

    controller.post("/rw/mastership?action=request")

    try:
        for task, modules in PROGRAM.items():
            for module in controller.program_modules(task):
                controller.post(f"/rw/rapid/tasks/{task}?action=unloadmod", {"module": module})

                print(f"{task}: unloaded {module}")

            for name in modules:
                path = controller.upload(name, (RAPID_DIR / name).read_text(encoding="utf-8"))

                controller.post(f"/rw/rapid/tasks/{task}?action=loadmod", {"modulepath": path, "replace": "true"})

                print(f"{task}: loaded {name}")

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
