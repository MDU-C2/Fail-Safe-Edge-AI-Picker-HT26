"""Windows checkout regression: preserve the deployed byte identities."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cup_inventory import SelectionError
from repair_reference_line_endings import repair_calibration
from table_reference_contract import validate_reference


ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "calibration/calibration_grid.json"
REFERENCE = ROOT / "capture_and_detect/placing_grid_outside_tray.table-reference.json"


def files(tmp_path, *, crlf=True):
    raw = CALIBRATION.read_bytes()
    assert b"\r" not in raw
    calibration, reference = tmp_path / "calibration.json", tmp_path / "reference.json"
    calibration.write_bytes(raw.replace(b"\n", b"\r\n") if crlf else raw)
    # A Windows reference checkout may also be CRLF. It must remain untouched.
    reference.write_bytes(REFERENCE.read_bytes().replace(b"\n", b"\r\n"))
    return calibration, reference, raw


def validate_files(calibration, reference, *, expected_identity=None):
    raw = calibration.read_bytes()
    values = json.loads(raw)
    artifact = json.loads(reference.read_bytes())
    identity = artifact["identity"] if expected_identity is None else expected_identity
    model = SimpleNamespace(digest=hashlib.sha256(raw).hexdigest(), scale=values["scale"],
                            R=values["R"], t=values["t"], offset=values.get("tag_offset", [0, 0, 0]))
    return validate_reference(artifact, calibration=model, intrinsics=identity["intrinsics"],
                              locations=identity["locations"], cell_size=identity["cell_size_mm"],
                              table_z=identity["table_z_mm"])


def test_windows_failure_and_exact_guarded_repair(tmp_path):
    calibration, reference, original = files(tmp_path)
    before_reference = reference.read_bytes()
    with pytest.raises(SelectionError, match="calibration_sha256"):
        validate_files(calibration, reference)
    assert repair_calibration(calibration, reference) == "repairable"
    assert b"\r\n" in calibration.read_bytes()
    assert repair_calibration(calibration, reference, apply=True) == "repaired"
    assert calibration.read_bytes() == original
    assert reference.read_bytes() == before_reference
    validate_files(calibration, reference)
    assert repair_calibration(calibration, reference, apply=True) == "unchanged"


def test_already_matching_bytes_are_not_rewritten(tmp_path):
    calibration, reference, original = files(tmp_path, crlf=False)
    with patch("repair_reference_line_endings.os.replace") as replace:
        assert repair_calibration(calibration, reference, apply=True) == "unchanged"
    replace.assert_not_called()
    assert calibration.read_bytes() == original


@pytest.mark.parametrize("change", ["translation", "offset", "format", "bom"])
def test_other_calibration_changes_refuse_repair(tmp_path, change):
    calibration, reference, original = files(tmp_path)
    if change in ("translation", "offset"):
        values = json.loads(original)
        values["t" if change == "translation" else "tag_offset"][0] += 1
        modified = json.dumps(values, indent=2).encode().replace(b"\n", b"\r\n")
    elif change == "format":
        modified = calibration.read_bytes() + b" "
    else:
        modified = b"\xef\xbb\xbf" + calibration.read_bytes()
    calibration.write_bytes(modified)
    with pytest.raises(ValueError, match="not a CRLF-only"):
        repair_calibration(calibration, reference, apply=True)
    assert calibration.read_bytes() == modified


def test_bad_reference_and_symlink_refuse_repair(tmp_path):
    calibration, reference, _ = files(tmp_path)
    before = calibration.read_bytes()
    reference.write_text("{}")
    with pytest.raises(ValueError, match="not a supported"):
        repair_calibration(calibration, reference, apply=True)
    assert calibration.read_bytes() == before
    reference.unlink()
    try:
        reference.symlink_to(REFERENCE)
    except OSError:
        pytest.skip("Symlink creation is unavailable for this test account")
    with pytest.raises(ValueError, match="regular JSON"):
        repair_calibration(calibration, reference, apply=True)
    assert calibration.read_bytes() == before


def test_noncanonical_reference_digest_is_not_silently_accepted(tmp_path):
    calibration, reference, _ = files(tmp_path)
    before = calibration.read_bytes()
    value = json.loads(reference.read_bytes())
    value["identity"]["calibration_sha256"] = value["identity"]["calibration_sha256"].upper()
    reference.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="lowercase"):
        repair_calibration(calibration, reference, apply=True)
    assert calibration.read_bytes() == before


def test_concurrent_file_change_is_preserved(tmp_path):
    calibration, reference, _ = files(tmp_path)
    changed = b'{"operator_edit": true}\n'
    real_chmod = __import__("os").chmod

    def edit_while_preparing(path, mode):
        real_chmod(path, mode)
        calibration.write_bytes(changed)

    with patch("repair_reference_line_endings.os.chmod", side_effect=edit_while_preparing):
        with pytest.raises(ValueError, match="changed during"):
            repair_calibration(calibration, reference, apply=True)
    assert calibration.read_bytes() == changed
    assert not list(tmp_path.glob(".calibration-lf-*"))


def test_repair_does_not_accept_a_real_camera_or_grid_change(tmp_path):
    calibration, reference, _ = files(tmp_path)
    repair_calibration(calibration, reference, apply=True)
    artifact = json.loads(reference.read_bytes())
    for field in ("intrinsics", "locations"):
        changed = copy.deepcopy(artifact)
        if field == "intrinsics":
            changed["identity"][field]["fx"] += 1
        else:
            changed["identity"][field][0]["x"] += 1
        # The helper changes no reference geometry, even after calibration repair.
        reference.write_text(json.dumps(changed))
        before = reference.read_bytes()
        assert repair_calibration(calibration, reference, apply=True) == "unchanged"
        assert reference.read_bytes() == before
        with pytest.raises(SelectionError, match="intrinsics|locations"):
            validate_files(calibration, reference, expected_identity=artifact["identity"])


def test_git_windows_checkout_preserves_both_reference_identities(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for the checkout regression")
    repo, checkout = tmp_path / "repo", tmp_path / "checkout"
    repo.mkdir()
    checkout.mkdir()
    subprocess.run([git, "init", "-q", str(repo)], check=True)
    paths = ["calibration/calibration_grid.json",
             "capture_and_detect/placing_grid_outside_tray.table-reference.json"]
    (repo / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    for name in paths:
        target = repo / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    command = [git, "-C", str(repo), "-c", "core.autocrlf=true", "-c", "core.safecrlf=false"]
    subprocess.run(command + ["add", ".gitattributes", *paths], check=True, capture_output=True)
    subprocess.run(command + ["checkout-index", "--all", "--prefix=" + checkout.as_posix() + "/"],
                   check=True, capture_output=True)
    for name in paths:
        assert (checkout / name).read_bytes() == (ROOT / name).read_bytes()
