"""Offline integration checks for the selectable Python picker service."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

import picker_workflow as picker
from cup_commands import CupSession
from device_agent import ExecutionLock
from device_service import arguments, python_factory
from placing_grid import PlacingGrid
from robot_client import RobotClient
from web_app import hardware_factory


@pytest.fixture
def python_rig(tmp_path, monkeypatch):
    grid_path = tmp_path / "placing-grid.json"
    grid_path.write_text(json.dumps({"cell_size_mm": 90, "locations": [
        {"id": 2, "x": 400, "y": 0, "z": 230, "occupied": True,
         "reserved": True, "batch_id": "existing-batch"},
        {"id": 3, "x": 500, "y": 0, "z": 230, "occupied": True}]}))
    (tmp_path / "gateway-token").write_text("offline-gateway-token")
    (tmp_path / "captures").mkdir()
    K = {"width": 640, "height": 480, "fx": 600.0, "fy": 600.0,
         "cx": 320.0, "cy": 240.0}
    calibration = SimpleNamespace(scale=1.0, R=np.diag([1.0, -1.0, -1.0]),
        t=np.array([515.0, 0.0, 960.0]), offset=(-115.0, 0.0, 40.0),
        digest="offline-calibration", ensure_unchanged=Mock())
    frames = []
    def capture(**kwargs):
        sequence = 10 + len(frames)
        frame = {"sequence": sequence, "timestamp": "2026-11-13T12:00:00Z",
                 "observed_at": "2026-11-13T12:00:00Z",
                 "received_monotonic": 100.1 + sequence,
                 "observation_started_monotonic": 100.0 + sequence}
        rgb = np.full((480, 640, 3), sequence, dtype=np.uint8)
        depth = np.full((480, 640), 1000, dtype=np.uint16)
        frames.append((rgb, depth, frame))
        return rgb, depth, frame
    camera = SimpleNamespace(api="http://offline.invalid:8000/api/v1",
        intrinsics=Mock(return_value=copy.deepcopy(K)), capture=Mock(side_effect=capture))
    sample_counts = []
    detector = SimpleNamespace(calibration=calibration, K=K, conf=0.5)
    def analyse(rgb, depth):
        sample_counts.append(len(detector._depth_samples) or 1)
        return [], rgb.copy()
    detector.analyse = Mock(side_effect=analyse)
    store = SimpleNamespace(folder=tmp_path / "captures", save_capture=Mock(), save=Mock())
    create_detector = Mock(return_value=(detector, Path("/offline/best.pt")))
    create_robot = Mock(return_value=RobotClient(camera.api, {}, dry_run=True))
    monkeypatch.setattr(picker, "load_token", lambda: "offline-gateway-token")
    monkeypatch.setattr(picker, "Calibration", Mock(return_value=calibration))
    monkeypatch.setattr(picker, "CaptureStore", Mock(return_value=store))
    monkeypatch.setattr("camera_client.CameraClient", Mock(return_value=camera))
    monkeypatch.setattr("inference_integration.create_detector", create_detector)
    monkeypatch.setattr("inference_integration.create_robot", create_robot)
    monkeypatch.setattr("requests.sessions.Session.request",
                        Mock(side_effect=AssertionError("Offline test attempted network IO")))
    monkeypatch.setenv("GATEWAY_TOKEN", "test-original-environment")
    args = arguments(["--device-id", "mac", "--backend", "python",
        "--state-dir", str(tmp_path), "--model", "/offline/best.pt",
        "--token-file", str(tmp_path / "device-token"), "--robot-id", "offline_robot_01",
        "--gateway", "http://offline.invalid:8000"])
    resources = []
    controller = SimpleNamespace(event=Mock(), attach_resource=resources.append,
        publish_inference=Mock(), publish_session=Mock())
    yield SimpleNamespace(root=tmp_path, args=args, grid=PlacingGrid(grid_path),
        calibration=calibration, camera=camera, detector=detector, store=store,
        frames=frames, sample_counts=sample_counts, controller=controller,
        create_detector=create_detector, create_robot=create_robot)
    for resource in resources:
        resource.close()


def test_python_service_import_and_factory_creation_defer_heavy_dependencies():
    code = '''
import importlib.abc, pathlib, sys
from types import SimpleNamespace
sys.path.insert(0, str(pathlib.Path('capture_and_detect').resolve()))
blocked = {'numpy', 'cv2', 'torch', 'ultralytics', 'fastapi', 'uvicorn', 'depthai'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('Premature dependency: ' + fullname)
sys.meta_path.insert(0, Guard())
from device_service import python_factory
factory = python_factory(SimpleNamespace(state_dir=pathlib.Path('/absent'),
    model=pathlib.Path('/absent/model.pt'), max_result_age=60.0, depth_frames=5))
assert factory.metadata()['backend'] == 'python'
assert 'web_app' not in sys.modules
assert 'capture_and_detect' not in sys.modules
assert 'picker_workflow' not in sys.modules
assert 'inference_integration' not in sys.modules
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parent.parent)
    assert result.returncode == 0, result.stderr


def test_python_factory_maps_standard_state_and_single_pass_policy_only_when_activated(python_rig):
    rig = python_rig
    sentinel = (object(), object())
    inner = Mock(return_value=sentinel)
    with patch("web_app.hardware_factory", return_value=inner) as hardware:
        factory = python_factory(rig.args)
        hardware.assert_not_called()
        assert not rig.create_detector.called and rig.camera.capture.call_count == 0
        assert os.environ["GATEWAY_TOKEN"] == "test-original-environment"
        assert factory(rig.controller) == sentinel
    options = hardware.call_args.args[0]
    inner.assert_called_once_with(rig.controller)
    assert options.output == str(rig.root / "captures")
    assert options.calibration == str(rig.root / "calibration.json")
    assert options.placing_grid == str(rig.root / "placing-grid.json")
    assert options.inference_model == "/offline/best.pt"
    assert options.isolated_inference and options.mac_inference_url is None
    assert options.inference_image_size == 640 and options.inference_threads == 3
    assert options.execution_lock_held is True
    assert options.discover_operator_controls is True
    assert options.controller_profile == rig.root / "controller-status.json"
    assert options.scan_observations == options.verification_frames == 1
    assert options.strict_single_verification is True and options.max_result_age == 60
    assert options.between_cup_check is True
    assert options.depth_frames == 5 and not options.llm and not options.live
    assert os.environ["GATEWAY_TOKEN"] == "offline-gateway-token"
    assert "offline-gateway-token" not in json.dumps(factory.metadata())
    assert factory.metadata()["workflow"] == "workspace_between_cups"
    assert factory.metadata()["per_cup_capture"] is True


@pytest.mark.parametrize("host", ["Darwin", "Linux", "Windows"])
def test_direct_ui_enables_workspace_check_only_on_mac_host(python_rig, monkeypatch, host):
    rig = python_rig
    monkeypatch.setattr("web_app.platform.system", lambda: host)
    options = SimpleNamespace(gateway=rig.args.gateway, calibration=str(rig.root / "calibration.json"),
        output=str(rig.store.folder), placing_grid=str(rig.grid.path), grid_table_z=0.0,
        grid_cell_size=80.0, depth_frames=1, speed=None, max_result_age=15.0,
        match_mm=20.0, match_px=40.0)
    session, _ = hardware_factory(options)(rig.controller)
    expected = host == "Darwin"
    assert session.between_cup_check is expected
    assert session.between_cup_grid_check is expected
    assert session.motion_settings()["between_cup_check"] is expected
    assert session.motion_settings()["workflow"] == ("workspace_between_cups" if expected else "v4_snapshot")
    assert session.motion_settings()["between_cup_grid_check"] is expected
    assert session.home_after_cup is False
    assert not rig.camera.capture.called and not rig.detector.analyse.called


@pytest.mark.parametrize("host", ["Darwin", "Linux", "Windows"])
@pytest.mark.parametrize("device", ["mac", "pi"])
def test_managed_device_policy_overrides_host_and_matches_metadata(python_rig, monkeypatch, host, device):
    rig = python_rig
    monkeypatch.setattr("web_app.platform.system", lambda: host)
    rig.args.device_id = device
    factory = python_factory(rig.args)
    session, _ = factory(rig.controller)
    expected = device == "mac"
    workflow = "workspace_between_cups" if expected else "v4_snapshot"
    assert session.between_cup_check is expected
    assert session.between_cup_grid_check is expected
    assert session.motion_settings()["workflow"] == workflow
    assert factory.metadata()["workflow"] == workflow
    assert factory.metadata()["per_cup_capture"] is expected
    assert session.motion_settings()["between_cup_grid_check"] is expected
    assert session.home_after_cup is False
    assert not rig.camera.capture.called and not rig.detector.analyse.called


def test_managed_factory_requests_discovery_even_when_profile_explicitly_none(python_rig):
    rig = python_rig
    rig.args.controller_profile = None
    with patch("web_app.hardware_factory", return_value=Mock(return_value=(None, None))) as hardware:
        python_factory(rig.args)(rig.controller)
    options = hardware.call_args.args[0]
    assert options.controller_profile is None and options.discover_operator_controls is True


@pytest.mark.parametrize("backend,device", [("python", "mac"), ("python", "pi"), ("native", "lynx")])
def test_service_profile_defaults_to_state_directory_without_requiring_file(python_rig, backend, device):
    rig = python_rig
    command = ["--device-id", device, "--backend", backend, "--state-dir", str(rig.root),
        "--model", "/offline/model", "--token-file", "/offline/device-token", "--robot-id", "offline_robot_01"]
    default = arguments(command)
    assert default.controller_profile == rig.root.resolve() / "controller-status.json"
    assert not default.controller_profile.exists()
    explicit = arguments(command + ["--controller-profile", "custom-status.json"])
    assert explicit.controller_profile == Path("custom-status.json")


def test_legacy_execution_context_does_not_block_cli_initialization(python_rig):
    rig = python_rig
    path = rig.root / ".placing-grid.json.controller.lock"
    with ExecutionLock(path), patch.object(picker, "load_token", side_effect=ValueError("Offline credential sentinel")) as token, \
            patch.object(picker, "CameraClient") as camera, \
            redirect_stdout(io.StringIO()) as output:
        result = picker.main(["--placing-grid", str(rig.grid.path),
            "--calibration", str(rig.root / "calibration.json"),
            "--output", str(rig.store.folder), "--gateway", "http://offline.invalid:8000", "--dry-run"])
    assert result == 1 and "Offline credential sentinel" in output.getvalue()
    token.assert_called_once_with()
    camera.assert_not_called()
    rig.create_detector.assert_not_called()
    assert not path.exists()


def test_python_service_uses_one_yolo_pass_five_raw_samples_and_preserves_grid(python_rig):
    from placement_records import combine
    rig = python_rig
    before = rig.grid.path.read_bytes()
    snapshot = rig.grid.snapshot()
    # Legacy context handles remain compatible without claiming OS ownership.
    with ExecutionLock(rig.root / ".placing-grid.json.controller.lock"):
        session, language = python_factory(rig.args)(rig.controller)
        assert session.between_cup_check is True
        assert session.between_cup_grid_check is True
        assert session.verification_frames == 1 and session.strict_single_verification
        assert session.max_result_age == 60 and session.robot.dry_run
        assert language.cup_matcher is None
        rig.create_detector.assert_called_once()
        assert not rig.camera.capture.called and not rig.detector.analyse.called
        with patch("placement_records.combine", wraps=combine) as common_combine:
            inventory, started = session.capture("scan")
        common_combine.assert_called_once()
    observation = inventory["placement_observation"]
    assert rig.camera.capture.call_count == 5 and rig.detector.analyse.call_count == 1
    assert rig.sample_counts == [5]
    assert observation["frame_count"] == 1 and observation["frame_sequences"] == [14]
    assert observation["grid_snapshot"] == snapshot
    assert not observation["applied"] and observation["released_ids"] == []
    assert "one fresh" in observation["message"] and "three" not in observation["message"].lower()
    assert inventory["frame"]["depth_sampling"]["count"] == 5
    assert inventory["frame"]["depth_sampling"]["sequences"] == [10, 11, 12, 13, 14]
    assert started == rig.frames[0][2]["observation_started_monotonic"]
    assert observation["cell_size_mm"] == 90
    assert [slot["state"] for slot in observation["slots"]] == ["clear", "clear"]
    for slot in observation["slots"]:
        assert slot["cup_count"] == 0 and slot["assigned_cups"] == []
        assert slot["evidence"] == [] and len(slot["polygon_px"]) >= 3
        assert slot["valid_fraction"] == slot["table_fraction"] == 1
    saved, rgb, depth, *_ = rig.store.save_capture.call_args.args
    assert saved is inventory and rgb is rig.frames[-1][0] and depth is rig.frames[-1][1]
    assert rig.grid.path.read_bytes() == before
    assert session.recovery.path == rig.store.folder / "robot_recovery.json"
    session.inventory = inventory
    session.save(inventory)
    rig.store.save.assert_called_once_with(inventory)
    rig.controller.publish_session.assert_called_once()


@pytest.mark.parametrize("legacy_verification_frames", [None, 3])
def test_existing_web_factory_uses_one_snapshot_observation(python_rig, legacy_verification_frames):
    rig = python_rig
    options = SimpleNamespace(gateway=rig.args.gateway, calibration=str(rig.root / "calibration.json"),
        output=str(rig.store.folder), placing_grid=str(rig.grid.path), grid_table_z=0.0,
        grid_cell_size=80.0, depth_frames=1, speed=None, max_result_age=15.0,
        match_mm=20.0, match_px=40.0, llm=False, llm_model="unused", ollama_url="http://127.0.0.1:11434",
        between_cup_check=False)
    if legacy_verification_frames is not None:
        options.verification_frames = legacy_verification_frames
    before = rig.grid.path.read_bytes()
    session, _ = hardware_factory(options)(rig.controller)
    assert session.motion_settings()["workflow"] == "v4_snapshot"
    assert session.between_cup_grid_check is False
    assert session.motion_settings()["between_cup_grid_check"] is False
    assert session.require_placement_evidence is True
    assert session.max_result_age == 15
    inventory, _ = session.capture("scan")
    assert rig.camera.capture.call_count == rig.detector.analyse.call_count == 1
    assert rig.sample_counts == [1]
    assert inventory["placement_observation"]["frame_count"] == 1
    assert inventory["placement_observation"]["frame_sequences"] == [10]
    assert rig.grid.path.read_bytes() == before


@pytest.mark.parametrize("workspace_check", [False, True])
def test_session_grid_refresh_remains_opt_in_for_legacy_and_source_only_callers(tmp_path, workspace_check):
    capture, save = Mock(), Mock()
    session = CupSession(capture, save, RobotClient("http://offline.invalid/api/v1", {}, dry_run=True),
                         "offline-calibration", placing_grid=PlacingGrid(tmp_path / "grid.json"),
                         between_cup_check=workspace_check)
    assert session.between_cup_check is workspace_check
    assert session.between_cup_grid_check is False
    assert session.motion_settings()["between_cup_grid_check"] is False
    capture.assert_not_called()
    save.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", [], {}])
def test_session_grid_refresh_requires_a_real_boolean(tmp_path, invalid):
    capture, save = Mock(), Mock()
    with pytest.raises(ValueError, match="between_cup_grid_check must be a boolean"):
        CupSession(capture, save, Mock(), "offline-calibration", between_cup_check=True,
                   placing_grid=PlacingGrid(tmp_path / "grid.json"), between_cup_grid_check=invalid)
    capture.assert_not_called()
    save.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("workspace_check,has_grid", [(False, False), (False, True), (True, False)])
def test_session_grid_refresh_requires_workspace_refresh_and_a_grid(tmp_path, workspace_check, has_grid):
    capture, save = Mock(), Mock()
    grid = PlacingGrid(tmp_path / "grid.json") if has_grid else None
    with pytest.raises(ValueError, match="require workspace checks and a placement grid"):
        CupSession(capture, save, Mock(), "offline-calibration", between_cup_check=workspace_check,
                   placing_grid=grid, between_cup_grid_check=True)
    capture.assert_not_called()
    save.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid", [False, True, 0, 2, 4, 1.0, "1"])
def test_python_capture_rejects_ambiguous_observation_count(python_rig, invalid):
    rig = python_rig
    with pytest.raises(ValueError, match="scan_observations must be 1 or 3"):
        picker.CaptureService(rig.camera, rig.detector, rig.store, "offline.pt",
                              placing_grid=rig.grid, scan_observations=invalid)
    assert not rig.camera.capture.called and not rig.detector.analyse.called
