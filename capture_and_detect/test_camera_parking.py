"""Offline saved-pose validation and dashboard startup wiring."""

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from camera_parking import load_right_park_pose
from cup_inventory import SelectionError
from handover_plan import right_via_pose, right_camera_park_pose


GATEWAY = "http://offline.invalid:8000"
# Offline fixture only; no test pose is a deployment default.
POSE = {"x": 380, "y": -350, "z": 600, "rx": 170, "ry": -15, "rz": 25}


@pytest.fixture
def record():
    return {"schema_version": 1, "recorded_at": "2026-11-20T12:00:00Z",
            "gateway": GATEWAY, "robot_id": "offline-robot", "source": "operator_taught",
            "pose": copy.deepcopy(POSE), "controller_pose": {
                "arm": "right", "tool": "tool0", "wobj": "wobj0", "coordinate": "Wobj",
                "mech_unit": "ROB_R", "x": 380, "y": -350, "z": 600,
                "q1": 0, "q2": 1, "q3": 0, "q4": 0,
                "cf1": 0, "cf4": 0, "cf6": 0, "cfx": 0, "arm_angle": 0}}


@pytest.fixture
def preset_record():
    return {"schema_version": 1, "gateway": GATEWAY, "preset": "right_via"}


def save(tmp_path, record):
    path = tmp_path / "placing-grid.right-park.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_missing_sidecar_returns_none_without_creating_any_files(tmp_path):
    assert load_right_park_pose(tmp_path / "missing" / "park.json", GATEWAY) is None
    assert list(tmp_path.iterdir()) == []


def test_valid_record_returns_only_a_copied_normalized_pose(tmp_path, record):
    path = save(tmp_path, record)
    original = path.read_bytes()
    pose = load_right_park_pose(str(path), GATEWAY + "/")
    assert pose == POSE
    pose["x"] += 10
    assert load_right_park_pose(path, GATEWAY) == POSE
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_right_via_preset_loads_without_taught_coordinates_and_returns_fresh_copy(tmp_path, preset_record):
    path = save(tmp_path, preset_record)
    original = path.read_bytes()
    pose = load_right_park_pose(path, GATEWAY + "/")
    assert pose == right_via_pose()
    pose["x"] += 50
    assert load_right_park_pose(path, GATEWAY) == right_via_pose()
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("preset", [None, True, False, 1, [], {}, "", "RIGHT_VIA", "right_via ", "home"])
def test_preset_must_be_exact_supported_name(tmp_path, preset_record, preset):
    preset_record["preset"] = preset
    with pytest.raises(SelectionError):
        load_right_park_pose(save(tmp_path, preset_record), GATEWAY)


@pytest.mark.parametrize("field", ["pose", "controller_pose"])
@pytest.mark.parametrize("present_value", [None, {}, "unused"])
def test_preset_cannot_be_mixed_with_any_taught_pose_fields(tmp_path, preset_record, field, present_value):
    preset_record[field] = present_value
    with pytest.raises(SelectionError):
        load_right_park_pose(save(tmp_path, preset_record), GATEWAY)


def test_preset_cannot_override_an_otherwise_valid_taught_record(tmp_path, record):
    record["preset"] = "right_via"
    with pytest.raises(SelectionError):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("field,value", [("schema_version", None), ("schema_version", True),
                                        ("schema_version", 2), ("gateway", None),
                                        ("gateway", "http://another.invalid:8000")])
def test_preset_still_requires_supported_schema_and_matching_gateway(tmp_path, preset_record, field, value):
    preset_record[field] = value
    with pytest.raises(SelectionError):
        load_right_park_pose(save(tmp_path, preset_record), GATEWAY)


@pytest.mark.parametrize("value", [None, [], "record", True, 1])
def test_non_object_record_is_rejected(tmp_path, value):
    with pytest.raises(SelectionError, match="object"):
        load_right_park_pose(save(tmp_path, value), GATEWAY)


@pytest.mark.parametrize("version", [None, True, False, "1", 1.0, 0, 2])
def test_schema_version_requires_integer_one(tmp_path, record, version):
    record["schema_version"] = version
    with pytest.raises(SelectionError, match="schema_version 1"):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("gateway", [None, "", GATEWAY + "/", "http://another.invalid:8000",
                                     GATEWAY + "/api/v1", {"origin": GATEWAY}])
def test_record_gateway_must_match_configured_origin(tmp_path, record, gateway):
    record["gateway"] = gateway
    with pytest.raises(SelectionError, match="configured gateway"):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("field,value", [("arm", "left"), ("tool", "gripper"),
                                        ("wobj", "table"), ("coordinate", "Base"),
                                        ("coordinate", "wobj"), ("arm", None)])
def test_wrong_controller_frame_is_rejected(tmp_path, record, field, value):
    record["controller_pose"][field] = value
    with pytest.raises(SelectionError, match="controller_pose"):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("field", ["arm", "tool", "wobj", "coordinate"])
def test_missing_controller_frame_field_is_rejected(tmp_path, record, field):
    del record["controller_pose"][field]
    with pytest.raises(SelectionError, match="controller_pose"):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("value", [None, [], "right", True])
def test_invalid_controller_record_is_rejected(tmp_path, record, value):
    record["controller_pose"] = value
    with pytest.raises(SelectionError, match="controller_pose"):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("field", ["schema_version", "gateway", "pose", "controller_pose"])
def test_missing_required_record_field_is_rejected(tmp_path, record, field):
    del record[field]
    with pytest.raises(SelectionError):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("pose", [None, [], {}, {**POSE, "extra": 0}, {**POSE, "x": True},
                                  {**POSE, "rz": "25"}, {**POSE, "y": -455},
                                  {**POSE, "z": 198}])
def test_invalid_pose_and_existing_endpoint_limits_are_rejected(tmp_path, record, pose):
    record["pose"] = pose
    with pytest.raises(SelectionError):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


@pytest.mark.parametrize("raw", ["", "{", "{} trailing", '{"schema_version":1,"schema_version":1}',
                                 '{"metadata":{"secret":1,"secret":2}}',
                                 '{"metadata":NaN}', '{"metadata":Infinity}',
                                 '{"metadata":-Infinity}', '{"metadata":1e9999}'])
def test_json_is_strict_including_nested_metadata(tmp_path, raw):
    path = tmp_path / "park.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(SelectionError, match="valid JSON"):
        load_right_park_pose(path, GATEWAY)


def test_unreadable_or_non_utf8_files_are_not_treated_as_absent(tmp_path):
    with pytest.raises(SelectionError, match="Cannot read"):
        load_right_park_pose(tmp_path, GATEWAY)
    path = tmp_path / "park.json"
    path.write_bytes(b"\xff")
    with pytest.raises(SelectionError, match="Cannot read"):
        load_right_park_pose(path, GATEWAY)
    with patch.object(Path, "read_text", side_effect=PermissionError("private-secret")):
        with pytest.raises(SelectionError, match="Cannot read") as error:
            load_right_park_pose(path, GATEWAY)
    assert "private-secret" not in str(error.value)


def test_gateway_mismatch_error_does_not_disclose_saved_values(tmp_path, record):
    record["gateway"] = "http://private-user:private-secret@offline.invalid:8000"
    with pytest.raises(SelectionError) as error:
        load_right_park_pose(save(tmp_path, record), GATEWAY)
    assert "private-user" not in str(error.value)
    assert "private-secret" not in str(error.value)
    assert GATEWAY not in str(error.value)


@pytest.fixture
def factory_rig(tmp_path, monkeypatch):
    """Exercise the real factory/session with camera, model and robot IO replaced."""
    from robot_client import RobotClient
    root = tmp_path / "captures"
    root.mkdir()
    store = SimpleNamespace(folder=root, save=Mock())
    calibration = SimpleNamespace(digest="offline-calibration", offset=(0, 0, 0))
    camera = Mock()
    captures = Mock()
    robot = RobotClient(GATEWAY + "/api/v1", {}, dry_run=True)
    create_robot = Mock(return_value=robot)
    monkeypatch.setitem(sys.modules, "picker_workflow", SimpleNamespace(
        load_token=lambda: "offline-token", Calibration=Mock(return_value=calibration),
        CaptureStore=Mock(return_value=store), CaptureService=Mock(return_value=captures)))
    monkeypatch.setitem(sys.modules, "inference_integration", SimpleNamespace(
        create_detector=Mock(return_value=(Mock(), "offline.pt")), create_robot=create_robot))
    from camera_client import TransientCameraError
    monkeypatch.setitem(sys.modules, "camera_client", SimpleNamespace(
        CameraClient=Mock(return_value=camera), TransientCameraError=TransientCameraError))
    request = Mock(side_effect=AssertionError("Network forbidden in offline test"))
    monkeypatch.setattr("requests.sessions.Session.request", request)
    args = SimpleNamespace(gateway=GATEWAY, calibration="offline.json", output=str(root),
        placing_grid=str(tmp_path / "placing-grid.json"), grid_table_z=0.0, grid_cell_size=90.0,
        speed=None, max_result_age=15.0, match_mm=20.0, match_px=40.0, execution_lock_held=True)
    yield SimpleNamespace(args=args, controller=Mock(), camera=camera, captures=captures,
                          create_robot=create_robot)
    captures.capture.assert_not_called()
    request.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
def test_factory_loads_sidecar_only_for_enabled_between_cup_checks(factory_rig, enabled):
    from web_app import hardware_factory
    rig = factory_rig
    rig.args.between_cup_check = enabled
    rig.args.right_park = enabled
    with patch("camera_parking.load_right_park_pose", return_value=POSE) as loader:
        session, _ = hardware_factory(rig.args)(rig.controller)
    assert session.between_cup_check is enabled
    assert session.between_cup_grid_check is enabled
    assert session.motion_settings()["between_cup_grid_check"] is enabled
    assert session.right_park_pose == (POSE if enabled else None)
    if enabled:
        loader.assert_called_once_with(Path(rig.args.placing_grid).with_suffix(".right-park.json"), GATEWAY)
    else:
        loader.assert_not_called()


def test_factory_passes_real_saved_pose_to_session_and_preserves_missing_default(factory_rig, record):
    from web_app import hardware_factory
    rig = factory_rig
    rig.args.between_cup_check = True
    session, _ = hardware_factory(rig.args)(rig.controller)
    assert session.right_park_pose is None
    save(Path(rig.args.placing_grid).parent, record)
    session, _ = hardware_factory(rig.args)(rig.controller)
    assert session.right_park_pose == POSE
    assert session.between_cup_grid_check is True
    assert session.home_after_cup is False


@pytest.mark.parametrize("enabled", [True, False])
def test_factory_passes_real_right_via_preset_only_when_checks_enabled(factory_rig, preset_record, enabled):
    from web_app import hardware_factory
    rig = factory_rig
    path = save(Path(rig.args.placing_grid).parent, preset_record)
    original = path.read_bytes()
    rig.args.between_cup_check = enabled
    rig.args.right_park = enabled
    session, _ = hardware_factory(rig.args)(rig.controller)
    assert session.between_cup_check is enabled
    assert session.between_cup_grid_check is enabled
    assert session.motion_settings()["between_cup_grid_check"] is enabled
    assert session.right_park_pose == (right_via_pose() if enabled else None)
    assert session.home_after_cup is False
    assert path.read_bytes() == original


def test_invalid_enabled_sidecar_prevents_robot_client_creation(factory_rig):
    from web_app import hardware_factory
    rig = factory_rig
    path = Path(rig.args.placing_grid).with_suffix(".right-park.json")
    path.write_text("malformed", encoding="utf-8")
    rig.args.between_cup_check = True
    with pytest.raises(SelectionError, match="valid JSON"):
        hardware_factory(rig.args)(rig.controller)
    rig.create_robot.assert_not_called()
    rig.args.between_cup_check = False
    rig.args.right_park = False
    session, _ = hardware_factory(rig.args)(rig.controller)
    assert session.right_park_pose is None
    assert session.between_cup_grid_check is False


def test_right_park_flag_loads_the_sidecar_without_workspace_checks(factory_rig):
    from web_app import hardware_factory
    rig = factory_rig
    rig.args.between_cup_check = False
    rig.args.right_park = True
    with patch("camera_parking.load_right_park_pose", return_value=POSE) as loader:
        session, _ = hardware_factory(rig.args)(rig.controller)
    loader.assert_called_once_with(Path(rig.args.placing_grid).with_suffix(".right-park.json"), GATEWAY)
    assert session.right_park_pose == POSE
    assert session.between_cup_check is False          # only the waiting position, no extra checks
    assert session.between_cup_grid_check is False


def test_shipped_outside_tray_waiting_position_loads_and_is_behind_the_grid():
    import json
    from camera_parking import load_right_park_pose
    here = Path(__file__).resolve().parent
    pose = load_right_park_pose(here / "placing_grid_outside_tray.right-park.json", "http://192.168.125.20:8000/")
    grid = json.loads((here / "placing_grid_outside_tray.json").read_text())
    cell = grid["cell_size_mm"]
    near_edge = min(place["x"] for place in grid["locations"]) - cell / 2
    assert pose == right_camera_park_pose()
    assert pose["x"] < near_edge   # behind the grid's near edge, toward the robot


def test_waiting_position_is_on_by_default_on_every_platform(factory_rig):
    import web_app
    from web_app import hardware_factory
    assert web_app.argparse.ArgumentParser  # web_app parses --right-park/--no-right-park
    rig = factory_rig
    rig.args.between_cup_check = False
    if hasattr(rig.args, "right_park"):
        del rig.args.right_park            # not given on the command line
    with patch("camera_parking.load_right_park_pose", return_value=POSE) as loader:
        session, _ = hardware_factory(rig.args)(rig.controller)
    loader.assert_called_once()
    assert session.right_park_pose == POSE
    assert session.between_cup_check is False


@pytest.mark.parametrize("gateway", ["http://192.168.125.20:8000", "http://169.254.219.127:8000/"])
def test_shipped_parking_accepts_only_explicit_gateway_origins(gateway):
    path = Path(__file__).with_name("placing_grid_outside_tray.right-park.json")
    assert load_right_park_pose(path, gateway) == right_camera_park_pose()


@pytest.mark.parametrize("gateway", ["http://169.254.219.128:8000", "http://192.168.125.20:8001",
                                      "https://192.168.125.20:8000", "http://localhost:8000"])
def test_shipped_parking_rejects_other_gateway_origins(gateway):
    path = Path(__file__).with_name("placing_grid_outside_tray.right-park.json")
    with pytest.raises(SelectionError, match="configured gateway"):
        load_right_park_pose(path, gateway)


@pytest.mark.parametrize("aliases", [None, True, {}, "http://alias.invalid:8000", [None],
    ["http://alias.invalid:8000", "http://alias.invalid:8000"], [GATEWAY],
    ["http://*.invalid:8000"], ["http://user:secret@alias.invalid:8000"],
    ["http://alias.invalid:8000/path"], ["http://alias.invalid:8000/"],
    ["http://alias.invalid:8000?query"], ["http://alias.invalid:8000#fragment"],
    ["ftp://alias.invalid:8000"], ["http://alias.invalid:65536"],
    ["http://alias.invalid:0"], ["http://alias.invalid:8000 "]])
def test_invalid_alias_configuration_is_rejected_even_for_canonical_gateway(tmp_path, record, aliases):
    record["gateway_aliases"] = aliases
    with pytest.raises(SelectionError, match="aliases"):
        load_right_park_pose(save(tmp_path, record), GATEWAY)


def test_workspace_checks_require_parking_even_with_disable_flag(factory_rig):
    from web_app import hardware_factory
    rig = factory_rig
    rig.args.between_cup_check, rig.args.right_park = True, False
    with patch("camera_parking.load_right_park_pose", return_value=POSE) as loader:
        session, _ = hardware_factory(rig.args)(rig.controller)
    loader.assert_called_once()
    assert session.right_park_pose == POSE
    assert session.between_cup_grid_check


def test_coordinated_mac_uses_shared_camera_target_without_loading_sidecar(factory_rig, tmp_path):
    from web_app import hardware_factory
    rig = factory_rig
    rig.args.live, rig.args.coordinator_url = True, "http://coordinator.invalid"
    rig.args.coordinator_token_file, rig.args.state_dir = tmp_path / "token", tmp_path
    rig.args.between_cup_check, rig.args.right_park = False, False
    with patch("device_service.read_gateway_token", return_value="offline"), \
         patch("failover_coordinator.CoordinatorClient"), \
         patch("failover_workflow.FailoverRunner"), \
         patch("camera_parking.load_right_park_pose") as loader:
        session, _ = hardware_factory(rig.args)(rig.controller)
    loader.assert_not_called()
    assert session.right_park_pose == right_camera_park_pose()
    assert session.between_cup_check and session.between_cup_grid_check


def test_pi_worker_uses_the_same_camera_target(factory_rig, tmp_path):
    from contextlib import ExitStack
    from failover_worker import build_session
    rig = factory_rig
    rig.args.state_dir, rig.args.model = tmp_path, tmp_path / "offline-model"
    rig.args.conf, rig.args.threads, rig.args.depth_frames = 0.5, 3, 5
    with ExitStack() as resources:
        session = build_session(rig.args, rig.create_robot.return_value, resources)
    assert session.right_park_pose == right_camera_park_pose()
    assert session.between_cup_check and session.between_cup_grid_check
