import hashlib
import json

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
