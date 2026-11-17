"""Check/repair a CRLF checkout only when its LF bytes match the saved calibration.

Run while the dashboard is closed. This does not contact hardware, rebuild a
table reference, change calibration numbers, or clear any placement/recovery state.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


HERE = Path(__file__).resolve().parent
MAX_FILE_BYTES = 1024 * 1024


def _read_regular(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise ValueError(f"Expected a regular JSON file under 1 MiB: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        current = path.lstat()
        if (not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError(f"JSON file changed while opening it: {path}")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"JSON file exceeds 1 MiB: {path}")
    return raw


def repair_calibration(calibration_path, reference_path, *, apply=False):
    """Return unchanged/repairable/repaired; refuse any non-line-ending mismatch."""
    calibration_path, reference_path = Path(calibration_path), Path(reference_path)
    raw, reference_raw = _read_regular(calibration_path), _read_regular(reference_path)
    reference = json.loads(reference_raw)
    if (not isinstance(reference, dict) or type(reference.get("schema_version")) is not int
            or reference["schema_version"] != 1
            or reference.get("kind") != "placement_table_reference"
            or reference.get("units") != "mm"
            or not isinstance(reference.get("identity"), dict)):
        raise ValueError("The supplied file is not a supported saved table reference.")
    expected = reference["identity"].get("calibration_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The saved table reference needs a valid lowercase calibration checksum.")
    if hashlib.sha256(raw).hexdigest() == expected:
        return "unchanged"
    normalized = raw.replace(b"\r\n", b"\n")
    if normalized == raw or hashlib.sha256(normalized).hexdigest() != expected:
        raise ValueError(
            "This is not a CRLF-only calibration mismatch. Nothing was changed. "
            "Check that the calibration and table reference are the correct pair; "
            "do not replace calibration values or rebuild from an occupied table."
        )
    if not apply:
        return "repairable"

    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=calibration_path.parent,
                                         prefix=".calibration-lf-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(normalized)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(calibration_path.stat().st_mode))
        # Recheck for operator edits before replacement. The dashboard must be closed.
        if (_read_regular(calibration_path) != raw
                or _read_regular(reference_path) != reference_raw):
            raise ValueError("Calibration or reference changed during the check. No repair was applied.")
        os.replace(temporary, calibration_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return "repaired"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path,
                        default=HERE.parent / "calibration" / "calibration_grid.json")
    parser.add_argument("--reference", type=Path,
                        default=HERE / "placing_grid_outside_tray.table-reference.json")
    parser.add_argument("--apply", action="store_true",
                        help="Repair only a proven CRLF-only checksum mismatch; close the dashboard first")
    args = parser.parse_args(argv)
    try:
        outcome = repair_calibration(args.calibration, args.reference, apply=args.apply)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Reference check failed: {error}\n")
    if outcome == "repairable":
        print("Confirmed: Windows CRLF line endings are the only calibration difference.")
        print("Close the dashboard, then rerun this command with --apply.")
    elif outcome == "repaired":
        print("Repaired calibration line endings. Its checksum now exactly matches the saved reference.")
        print("Calibration numbers, table reference, grid and recovery records were preserved.")
        print("Restart the dashboard and scan; live camera and grid checks still apply.")
    else:
        print("Calibration checksum already matches the saved reference. No files changed.")
        print("If scanning still fails, check the reported camera/grid differences and startup arguments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
