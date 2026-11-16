import hashlib
import json
import os
from pathlib import Path

import pytest

from lynx.model import Model
from lynx.prepare_model import prepare
from lynx.prepare_state import prepare as prepare_state


@pytest.fixture
def export(tmp_path):
    folder = tmp_path / "model"
    folder.mkdir()
    (folder / "model.ncnn.param").write_text("7767517\n3 3\nInput in0 0 1 in0\nReshape reshape 1 1 in0 out0 0=2100 1=4\nOutput out1 1 1 out0 out1\n")
    (folder / "model.ncnn.bin").write_bytes(b"weights")
    return folder


def test_manifest_checks_integrity_and_cannot_overwrite(export):
    prepare(export, 320, ["CUP", "CUP BASE", "CUP HANDLE", "TRAY"], "fp32")
    assert Model.load(export).input_size == 320
    with pytest.raises(FileExistsError):
        prepare(export, 320, ["CUP", "CUP BASE", "CUP HANDLE", "TRAY"], "fp32")
    (export / "model.ncnn.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        Model.load(export)


@pytest.mark.parametrize("size,names", [(640,["CUP","CUP BASE","CUP HANDLE"]),
    (320,["CUP","CUP BASE","CUP BASE"]), (320,["CUP","CUP BASE","CUP HANDLE","TRAY","extra"])])
def test_invalid_manifest_is_removed(export, size, names):
    with pytest.raises(ValueError):
        prepare(export, size, names, "fp32")
    assert not (export / "manifest.json").exists()


def test_state_preserves_grid_and_requires_first_use_recovery(tmp_path):
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"scale":1,"R":[[1,0,0],[0,1,0],[0,0,1]],"t":[0,0,0]}))
    grid = tmp_path / "grid.json"
    grid.write_text(json.dumps({"locations":[{"id":1,"x":400,"y":-200,"z":230,"occupied":True,"reserved":True}]}))
    state = prepare_state(tmp_path / "state", calibration, grid)
    assert (state / "placing-grid.json").read_bytes() == grid.read_bytes()
    assert (state / "calibration.json").read_bytes() == calibration.read_bytes()
    assert json.loads((state / "captures/robot_recovery.json").read_text())["recovery_required"] is True
    token = (state / "gateway-token").read_text().strip()
    registry = json.loads((state / "auth_clients.json").read_text())
    assert registry["lynx-picker"]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token != (state / "dashboard-token").read_text().strip()
    assert (state / "gateway-token").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exists"):
        prepare_state(state, calibration, grid)


@pytest.fixture
def grid_reference_inputs(tmp_path):
    """Use the committed, validated profile; never read the live grid records."""
    repo = Path(__file__).resolve().parents[2]
    calibration = tmp_path / "calibration.json"
    calibration.write_bytes((repo / "calibration/calibration_grid.json").read_bytes())
    reference = tmp_path / "grid.table-reference.json"
    reference.write_bytes((repo / "capture_and_detect/placing_grid_outside_tray.table-reference.json").read_bytes())
    identity = json.loads(reference.read_text())["identity"]
    grid = tmp_path / "grid.json"
    locations = [dict(place, occupied=False) for place in identity["locations"]]
    locations[0].update(occupied=True, reserved=True, reservation_id="preserve-this-reservation")
    grid.write_text(json.dumps({"cell_size_mm": identity["cell_size_mm"], "locations": locations}))
    return calibration, grid, reference


@pytest.mark.parametrize("explicit", [False, True])
def test_state_copies_verified_reference_and_keeps_occupied_records(tmp_path, grid_reference_inputs, explicit):
    calibration, grid, reference = grid_reference_inputs
    if explicit:
        reference = reference.rename(tmp_path / "selected-reference.json")
    inputs = {path: path.read_bytes() for path in (calibration, grid, reference)}
    state = prepare_state(tmp_path / "state", calibration, grid,
                          **({"table_reference": reference} if explicit else {}))
    assert (state / "placing-grid.table-reference.json").read_bytes() == inputs[reference]
    assert (state / "calibration.json").read_bytes() == inputs[calibration]
    assert (state / "placing-grid.json").read_bytes() == inputs[grid]
    assert all(path.read_bytes() == data for path, data in inputs.items())


@pytest.mark.parametrize("change", ["missing", "malformed", "oversized", "grid", "calibration", "intrinsics", "fit"])
def test_reference_must_validate_before_state_is_created(tmp_path, grid_reference_inputs, change):
    calibration, grid, reference = grid_reference_inputs
    if change == "missing":
        reference.unlink()
    elif change == "malformed":
        reference.write_text("null")
    elif change == "oversized":
        reference.write_bytes(b" " * 65537)
    elif change == "grid":
        value = json.loads(grid.read_text())
        value["locations"][0]["x"] += 1
        grid.write_text(json.dumps(value))
    elif change in ("calibration", "intrinsics"):
        value = json.loads(calibration.read_text())
        if change == "calibration":
            value["tag_offset"][2] -= 20
        else:
            value["intrinsics"]["fx"] += 1
        calibration.write_text(json.dumps(value))
        if change == "intrinsics":
            # Even an updated calibration digest cannot authorize a different
            # observed camera geometry in the frozen reference.
            ref = json.loads(reference.read_text())
            ref["identity"]["calibration_sha256"] = hashlib.sha256(calibration.read_bytes()).hexdigest()
            reference.write_text(json.dumps(ref))
    else:
        value = json.loads(reference.read_text())
        value["fit"]["rms_mm"] = 6
        reference.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        prepare_state(tmp_path / "state", calibration, grid, table_reference=reference)
    assert not (tmp_path / "state").exists()


def test_external_gateway_state_reuses_private_token_without_new_server_credentials(tmp_path, grid_reference_inputs):
    calibration, grid, reference = grid_reference_inputs
    token_path = tmp_path / "external-token"
    token_path.write_text("offline-existing-token\n")
    token_path.chmod(0o600)
    state = prepare_state(tmp_path / "state", calibration, grid, external_gateway_token_file=token_path)
    assert (state / "gateway-token").read_bytes() == token_path.read_bytes()
    assert (state / "gateway-token").stat().st_mode & 0o777 == 0o600
    assert not (state / "auth_clients.json").exists()
    assert not (state / "dashboard-token").exists()
    assert (state / "placing-grid.table-reference.json").read_bytes() == reference.read_bytes()
    assert json.loads((state / "captures/robot_recovery.json").read_text())["recovery_required"] is True


@pytest.mark.parametrize("invalid", ["symlink", "directory", "fifo", "public", "empty", "oversized", "whitespace", "nonascii"])
def test_external_token_rejected_before_any_state_write(tmp_path, grid_reference_inputs, invalid):
    calibration, grid, _ = grid_reference_inputs
    token_path = tmp_path / "external-token"
    token_path.write_bytes(b"offline-existing-token\n")
    token_path.chmod(0o600)
    if invalid == "symlink":
        target = token_path.rename(tmp_path / "real-token")
        token_path.symlink_to(target)
    elif invalid == "directory":
        token_path.unlink()
        token_path.mkdir(mode=0o700)
    elif invalid == "fifo":
        token_path.unlink()
        os.mkfifo(token_path, mode=0o600)
    elif invalid == "public":
        token_path.chmod(0o644)
    else:
        token_path.write_bytes({"empty": b" \n", "oversized": b"a" * 4097,
                                "whitespace": b"token with spaces", "nonascii": b"\xff"}[invalid])
    with pytest.raises(ValueError):
        prepare_state(tmp_path / "state", calibration, grid, external_gateway_token_file=token_path)
    assert not (tmp_path / "state").exists()
