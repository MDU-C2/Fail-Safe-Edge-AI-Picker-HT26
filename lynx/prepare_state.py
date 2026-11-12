"""Create a new deployment directory without changing existing robot state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil


def prepare(state_dir, calibration, placing_grid):
    from capture_and_detect import Calibration
    from placing_grid import PlacingGrid
    # Read and validate before creating anything. Never invent calibration or
    # empty occupied placement cells when moving an existing installation.
    Calibration(calibration)
    PlacingGrid(placing_grid).snapshot()
    state = Path(state_dir).resolve()
    if state.exists():
        raise ValueError("State directory already exists. Reuse it; never reinitialize persistent state.")
    state.mkdir(parents=True, mode=0o700)
    shutil.copyfile(calibration, state / "calibration.json")
    shutil.copyfile(placing_grid, state / "placing-grid.json")
    token = secrets.token_urlsafe(32)
    registry = {"lynx-picker": {"token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "permissions": ["camera.read", "robot.read", "robot.control"], "control_priority": 50}}
    for name, content in (("gateway-token", token + "\n"), ("dashboard-token", secrets.token_urlsafe(32) + "\n"),
                          ("auth_clients.json", json.dumps(registry, indent=2) + "\n")):
        with (state / name).open("x") as stream:
            stream.write(content)
        os.chmod(state / name, 0o600)
    captures = state / "captures"
    captures.mkdir()
    # A fresh host cannot prove completion of the previous host's last motion.
    # Require the existing operator recovery acknowledgement on first use.
    (captures / "robot_recovery.json").write_text(json.dumps({"schema_version": 1,
        "recovery_required": True, "context": {"source": "new_lynx_deployment"}}) + "\n")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--placing-grid", type=Path, required=True)
    args = parser.parse_args()
    state = prepare(args.state_dir, args.calibration, args.placing_grid)
    print(f"Prepared {state}; calibration and placement occupancy copied unchanged.")
    print("Credentials were saved privately. First use requires the existing recovery acknowledgement.")


if __name__ == "__main__":
    main()
