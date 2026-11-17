"""Create a new deployment directory without changing existing robot state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat


def _external_token(path):
    """Read an existing gateway credential without following links or exposing it."""
    try:
        # Nonblocking prevents a FIFO from hanging before fstat can reject it.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
                raise ValueError("External gateway token must be a private regular file (mode 0600 or 0400).")
            value = stream.read(4097)
        if len(value) > 4096:
            raise ValueError("External gateway token file exceeds its size limit.")
        token = value.decode("ascii").strip()
        if not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise ValueError("External gateway token must be nonempty printable ASCII without whitespace.")
        return token
    except (OSError, UnicodeError) as error:
        raise ValueError("Cannot read a private regular external gateway token file; symlinks are not accepted.") from error


def prepare(state_dir, calibration, placing_grid, *, table_reference=None,
            external_gateway_token_file=None):
    from picker_workflow import Calibration
    from placing_grid import PlacingGrid
    # Read and validate before creating anything. Never invent calibration or
    # empty occupied placement cells when moving an existing installation.
    calibration_path, grid_path = Path(calibration), Path(placing_grid)
    calibrated = Calibration(calibration_path)
    calibration_bytes = calibration_path.read_bytes()
    if hashlib.sha256(calibration_bytes).hexdigest() != calibrated.digest:
        raise ValueError("Calibration changed during state preparation; retry while idle.")
    grid_bytes = grid_path.read_bytes()
    snapshot = PlacingGrid(grid_path).snapshot()
    if grid_path.read_bytes() != grid_bytes:
        raise ValueError("Placement state changed during preparation; retry while idle.")
    reference_path = (Path(table_reference) if table_reference is not None
                      else grid_path.with_suffix(".table-reference.json"))
    reference_bytes = None
    try:
        with reference_path.open("rb") as stream:
            reference_bytes = stream.read(65537)
    except FileNotFoundError:
        if table_reference is not None:
            raise ValueError("The requested table reference is missing.") from None
    if reference_bytes is not None:
        from placement_table_reference import TableReference
        if len(reference_bytes) > 65536:
            raise ValueError("The table reference exceeds its size limit.")
        try:
            reference = json.loads(reference_bytes)
            intrinsics = json.loads(calibration_bytes).get("intrinsics")
            if not isinstance(intrinsics, dict):
                raise ValueError("The calibrated camera intrinsics are required for a table reference.")
            TableReference.from_dict(reference, calibration=calibrated, intrinsics=intrinsics,
                                     locations=snapshot["locations"],
                                     cell_size=snapshot.get("cell_size_mm", 80.0), table_z=0.0)
        except (UnicodeError, TypeError, KeyError) as error:
            raise ValueError("The table reference is invalid.") from error
    external_token = (_external_token(external_gateway_token_file)
                      if external_gateway_token_file is not None else None)
    state = Path(state_dir).resolve()
    if state.exists():
        raise ValueError("State directory already exists. Reuse it; never reinitialize persistent state.")
    state.mkdir(parents=True, mode=0o700)
    (state / "calibration.json").write_bytes(calibration_bytes)
    (state / "placing-grid.json").write_bytes(grid_bytes)
    if reference_bytes is not None:
        (state / "placing-grid.table-reference.json").write_bytes(reference_bytes)
    token = external_token if external_token is not None else secrets.token_urlsafe(32)
    credentials = [("gateway-token", token + "\n")]
    if external_token is None:
        registry = {"lynx-picker": {"token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "permissions": ["camera.read", "robot.read", "robot.control"], "control_priority": 50}}
        credentials.extend((("dashboard-token", secrets.token_urlsafe(32) + "\n"),
                            ("auth_clients.json", json.dumps(registry, indent=2) + "\n")))
    for name, content in credentials:
        descriptor = os.open(state / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
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
    parser.add_argument("--table-reference", type=Path,
                        help="Verified reference to copy; otherwise discover the selected grid's sidecar")
    parser.add_argument("--external-gateway-token-file", type=Path,
                        help="Private existing gateway credential for a picker-only deployment; no new gateway/dashboard credentials")
    args = parser.parse_args()
    state = prepare(args.state_dir, args.calibration, args.placing_grid,
                    table_reference=args.table_reference,
                    external_gateway_token_file=args.external_gateway_token_file)
    print(f"Prepared {state}; calibration and placement occupancy copied unchanged.")
    print("Credentials were saved privately. First use requires the existing recovery acknowledgement.")


if __name__ == "__main__":
    main()
