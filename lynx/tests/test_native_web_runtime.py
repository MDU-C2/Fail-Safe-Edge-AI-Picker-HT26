"""Offline native web-factory lifecycle, evidence and dependency checks."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lynx.runtime import native_web_factory
from lynx.tests.test_cli_capture import capture_rig


class Controller:
    def __init__(self):
        self.events, self.resources, self.published = [], [], 0
        self.on_publish = lambda: None

    def event(self, message, level="info"):
        self.events.append((message, level))

    def attach_resource(self, resource):
        self.resources.append(resource)

    def publish_session(self):
        self.on_publish()
        self.published += 1

    def close(self):
        for resource in self.resources:
            resource.close()


@pytest.fixture
def native_rig(capture_rig, monkeypatch):
    rig = capture_rig
    state = rig.grid.path.parent
    (state / "gateway-token").write_text("offline-test-token")
    rig.worker.model.input_size = 640
    rig.worker.close = Mock()
    rig.worker.process = SimpleNamespace(poll=Mock(return_value=None),
                                         stdin=SimpleNamespace(closed=False))
    rig.model_load = Mock(return_value=rig.worker.model)
    rig.worker_start = Mock(return_value=rig.worker)
    rig.camera_start = Mock(return_value=rig.camera)
    monkeypatch.setattr("lynx.model.Model.load", rig.model_load)
    monkeypatch.setattr("lynx.native_perception.NativePerception", rig.worker_start)
    monkeypatch.setattr("camera_client.CameraClient", rig.camera_start)
    # Exercise startup portably without consulting the host's real cgroups.
    monkeypatch.setattr("lynx.runtime.sys.platform", "darwin")
    def forbid_network(*args, **kwargs):
        raise AssertionError("Factory test attempted a network request")
    monkeypatch.setattr("requests.sessions.Session.request", forbid_network)
    rig.args = SimpleNamespace(state_dir=state, model=Path("/offline/model"))
    rig.controller = Controller()
    return rig


def test_import_and_factory_creation_do_not_load_heavy_modules_or_open_resources():
    code = '''
import importlib.abc, sys
from types import SimpleNamespace
blocked = {'numpy','cv2','torch','ultralytics','depthai','fastapi','uvicorn','gateway','starlette'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('Heavy dependency imported: ' + fullname)
sys.meta_path.insert(0, Guard())
import lynx.runtime
factory = lynx.runtime.native_web_factory(SimpleNamespace(state_dir='/does/not/exist', model='/missing'))
assert factory.metadata()['state'] == 'not_started'
assert not blocked.intersection(sys.modules)
assert 'lynx.native_perception' not in sys.modules
assert 'camera_client' not in sys.modules
from unittest.mock import patch
factory = lynx.runtime.native_web_factory(SimpleNamespace(state_dir='.', model='/missing'))
with patch('lynx.model.Model.load', side_effect=ValueError('before resources')):
    try:
        factory(None)
    except ValueError as error:
        assert str(error) == 'before resources'
    else:
        raise AssertionError('Expected mocked model load failure')
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_default_factory_matches_deployed_single_pass_policy_without_taking_file_lock(native_rig, monkeypatch):
    rig = native_rig
    original_grid = rig.grid.path.read_bytes()
    factory = native_web_factory(rig.args)
    assert not rig.worker_start.called and not rig.camera_start.called
    session, language = factory(rig.controller)
    assert session.verification_frames == 1 and session.strict_single_verification is True
    assert session.max_result_age == 60 and session.camera_z is False
    assert session.home_after_cup is False
    assert session.motion_settings()["home_after_cup"] is False
    assert session.robot.dry_run and session.robot.require_session is False
    assert session.placing_grid.path == rig.grid.path
    assert session.tag_offset == rig.calibration.offset
    assert session.recovery.path == rig.store.folder / "robot_recovery.json"
    assert language.named_cups is None and language.cup_matcher is None
    assert language.saved_cups()["enabled"] is False
    assert rig.camera.calls == 0 and not rig.worker.calls
    assert not (rig.grid.path.parent / "picker.lock").exists()
    assert not list(rig.grid.path.parent.glob("*.lock"))
    assert rig.grid.path.read_bytes() == original_grid
    rig.camera_start.assert_called_once_with("http://127.0.0.1:8000/api/v1",
        {"Authorization": "Bearer offline-test-token"}, max_dimensions=(1920, 1080))
    rig.worker_start.assert_called_once_with("/usr/local/bin/lynx-ncnn-worker", rig.worker.model,
                                           confidence=0.5, timeout=30.0)
    metadata = factory.metadata()
    assert metadata["ready"] and metadata["worker_running"]
    assert metadata["policy"] == {"workflow": "v4_snapshot", "scan_observations": 1,
        "verification_frames": 0, "per_cup_capture": False, "depth_frames": 5,
        "camera_z": False, "confidence_threshold": 0.5}
    assert metadata["model"]["sha256"] == rig.worker.model.sha256
    assert metadata["camera"] == rig.camera.K
    assert metadata["image_root"] == str(rig.store.folder)
    assert "offline-test-token" not in json.dumps(metadata)
    metadata["model"]["sha256"] = "forged"
    assert factory.metadata()["model"]["sha256"] == rig.worker.model.sha256
    rig.controller.close()
    rig.controller.close()
    rig.worker.close.assert_called_once()
    assert factory.metadata()["state"] == "closed" and not factory.metadata()["ready"]


def test_configured_motion_and_camera_settings_are_frozen_and_saved_before_publish(native_rig):
    rig = native_rig
    rig.args.gateway = "http://offline.invalid:8000/api/v1/"
    rig.args.depth_frames, rig.args.max_result_age = 3, 45.0
    rig.args.speed, rig.args.camera_z = 80, True
    rig.args.conf, rig.args.inference_timeout = 0.6, 20.0
    factory = native_web_factory(rig.args)
    rig.args.speed = 1000  # The agent cannot change an already-created runtime through its args.
    session, _ = factory(rig.controller)
    assert session.speed == 80 and session.camera_z is True and session.max_result_age == 45
    inventory, started = session.capture("scan")
    assert rig.camera.calls == 3 and len(rig.worker.calls) == 1
    assert started == 100.0
    assert inventory["frame"]["depth_sampling"]["count"] == 3
    assert inventory["placement_observation"]["frame_count"] == 1
    assert factory.metadata()["last_capture"] == {"capture_id": inventory["capture"],
        "purpose": "scan", "sequence": 3, "inference_ms": 2.0, "frame_count": 1}
    def assert_persisted():
        saved = json.loads((rig.store.folder / "cups.json").read_text())
        assert saved["cups"][0]["status"] == "movement_unconfirmed"
    rig.controller.on_publish = assert_persisted
    inventory["cups"][0]["status"] = "movement_unconfirmed"
    session.inventory = inventory
    session.save(inventory)
    assert rig.controller.published == 1
    rig.controller.close()


def test_linux_memory_guard_precedes_native_worker_and_exposes_only_measured_snapshot(native_rig, monkeypatch):
    rig = native_rig
    order = []
    monkeypatch.setattr("lynx.runtime.sys.platform", "linux")
    rig.model_load.side_effect = lambda *_args: order.append("model_verified") or rig.worker.model
    def advise_captures(folder, **options):
        assert folder == rig.store.folder and options == {"sync": True}
        order.append("capture_cache")
        return SimpleNamespace(summary=lambda: "Saved-image cache advice recorded")
    monkeypatch.setattr("lynx.resources.advise_capture_cache", advise_captures)
    monkeypatch.setattr("lynx.resources.advise_packaged_native_cache",
        lambda: order.append("native_cache") or SimpleNamespace(summary=lambda: "Native cache advice recorded"))
    monkeypatch.setattr("lynx.resources.advise_loaded_python_cache",
                        lambda: SimpleNamespace(summary=lambda: "Cache advice recorded"))
    monkeypatch.setattr("lynx.resources.trim_unused_heap", lambda: "Heap trim recorded")
    snapshot = SimpleNamespace(cgroup_headroom_bytes=150 * 1048576,
        host_available_bytes=250 * 1048576, summary=lambda required: f"Guard {required} MiB passed")
    guard = Mock(side_effect=lambda required: order.append("guard") or snapshot)
    monkeypatch.setattr("lynx.resources.wait_for_model_headroom", guard)
    rig.worker_start.side_effect = lambda *args, **kwargs: order.append("worker") or rig.worker
    factory = native_web_factory(rig.args)
    factory(rig.controller)
    guard.assert_called_once_with(128)
    assert order == ["model_verified", "capture_cache", "native_cache", "guard", "worker"]
    assert factory.metadata()["memory"] == {"guard_required_mib": 128, "checked": True,
        "cgroup_headroom_bytes": 150 * 1048576, "host_available_bytes": 250 * 1048576}
    rig.controller.close()


def test_model_verification_failure_prevents_native_cache_advice_and_worker(native_rig, monkeypatch):
    rig = native_rig
    rig.model_load.side_effect = ValueError("Model checksum mismatch")
    advice = Mock()
    monkeypatch.setattr("lynx.runtime.sys.platform", "linux")
    monkeypatch.setattr("lynx.resources.advise_packaged_native_cache", advice)
    with pytest.raises(ValueError, match="checksum mismatch"):
        native_web_factory(rig.args)(rig.controller)
    assert not advice.called and not rig.worker_start.called


def test_native_capture_advises_only_completed_rgb_and_depth_files(native_rig, monkeypatch):
    rig = native_rig
    advised = []
    def advise(folder, *, paths):
        assert folder == rig.store.folder
        assert len(paths) == 2 and all(Path(path).is_file() for path in paths)
        assert {Path(path).name.split("_", 1)[0] for path in paths} == {"rgb", "depth"}
        advised.append(paths)
    monkeypatch.setattr("lynx.resources.advise_capture_cache", advise)
    session, _ = native_web_factory(rig.args)(rig.controller)
    inventory, _ = session.capture("scan")
    assert advised == [(inventory["images"]["rgb"], inventory["images"]["depth"])]
    rig.controller.close()


def test_failed_memory_guard_never_starts_worker_or_camera_capture(native_rig, monkeypatch):
    rig = native_rig
    from lynx.resources import MemoryGuardError
    monkeypatch.setattr("lynx.runtime.sys.platform", "linux")
    monkeypatch.setattr("lynx.resources.advise_loaded_python_cache",
                        lambda: SimpleNamespace(summary=lambda: "Cache advice recorded"))
    monkeypatch.setattr("lynx.resources.trim_unused_heap", lambda: "Heap trim recorded")
    monkeypatch.setattr("lynx.resources.wait_for_model_headroom",
                        Mock(side_effect=MemoryGuardError("Insufficient test headroom")))
    factory = native_web_factory(rig.args)
    with pytest.raises(MemoryGuardError):
        factory(rig.controller)
    assert not rig.worker_start.called and not rig.controller.resources
    assert rig.camera.calls == 0 and factory.metadata()["state"] == "failed"


def test_invalid_reference_after_worker_start_closes_registered_worker(native_rig):
    rig = native_rig
    rig.reference_path.write_text("{}")
    factory = native_web_factory(rig.args)
    with pytest.raises(ValueError):
        factory(rig.controller)
    assert len(rig.controller.resources) == 1
    rig.worker.close.assert_called_once()
    assert factory.metadata()["state"] == "failed" and not factory.metadata()["worker_running"]
    rig.controller.close()
    rig.worker.close.assert_called_once()


def test_factory_cannot_start_a_second_native_worker(native_rig):
    rig = native_rig
    factory = native_web_factory(rig.args)
    factory(rig.controller)
    with pytest.raises(ValueError, match="already been started"):
        factory(rig.controller)
    rig.worker_start.assert_called_once()
    rig.controller.close()


def test_live_factory_preserves_explicit_mode_and_configures_only_requested_profile(native_rig, monkeypatch):
    rig = native_rig
    rig.args.live = True
    rig.args.controller_profile = Path("/offline/controller-profile.json")
    configure = Mock()
    monkeypatch.setattr("robot_client.RobotClient.configure_operator_controls", configure)
    factory = native_web_factory(rig.args)
    session, _ = factory(rig.controller)
    assert session.robot.dry_run is False
    assert session.home_after_cup is False
    configure.assert_called_once_with(rig.args.controller_profile)
    assert rig.camera.calls == 0 and not rig.worker.calls
    rig.controller.close()


def test_failed_camera_capture_invalidates_readiness_but_next_explicit_scan_can_recover(native_rig, monkeypatch):
    rig = native_rig
    factory = native_web_factory(rig.args)
    session, _ = factory(rig.controller)
    capture_raw = rig.camera.capture_raw
    with monkeypatch.context() as patch:
        patch.setattr(rig.camera, "capture_raw", Mock(side_effect=ValueError("Offline camera failure")))
        with pytest.raises(ValueError, match="Offline camera failure"):
            session.capture("scan")
    metadata = factory.metadata()
    assert metadata["state"] == "capture_failed" and not metadata["ready"]
    assert metadata["worker_running"] and metadata["error_type"] == "ValueError"
    assert not rig.worker.calls and not rig.worker.close.called
    assert rig.camera.capture_raw == capture_raw
    session.capture("scan")
    assert factory.metadata()["ready"] and factory.metadata()["error_type"] is None
    rig.controller.close()


def test_failed_inference_reports_closed_worker_without_hiding_original_error(native_rig, monkeypatch):
    rig = native_rig
    from lynx.native_transport import InferenceError
    factory = native_web_factory(rig.args)
    session, _ = factory(rig.controller)
    def fail_inference(*args):
        rig.worker.process.poll.return_value = 1
        rig.worker.process.stdin.closed = True
        raise InferenceError("Offline inference failure")
    monkeypatch.setattr(rig.worker, "observe", fail_inference)
    with pytest.raises(InferenceError, match="Offline inference failure"):
        session.capture("scan")
    metadata = factory.metadata()
    assert metadata["state"] == "capture_failed" and not metadata["ready"]
    assert not metadata["worker_running"] and metadata["error_type"] == "InferenceError"
    assert "last_capture" not in metadata
    rig.controller.close()


def test_real_web_controller_publishes_rgb_and_overlay_geometry_from_native_scan(native_rig):
    rig = native_rig
    from web_controller import WebController
    factory = native_web_factory(rig.args)
    controller = WebController(factory, mode="dry_run", model="Native INT8", llm_enabled=False)
    original_grid = rig.grid.path.read_bytes()
    try:
        controller.start()
        controller._worker.submit(lambda: None).result(timeout=3)
        state = controller.state()
        assert state["ready"] and not state["llm_enabled"]
        assert state["saved_cups"]["enabled"] is False
        controller.submit("scan", {"request_id": "offline_native_scan_01"})
        controller._worker.submit(lambda: None).result(timeout=3)
        state = controller.state()
        assert state["error"] is None and state["scan_usable"] and not state["busy"]
        inventory = state["inventory"]
        image = Path(controller.image(inventory["capture"]))
        assert image.parent == rig.store.folder and image.suffix == ".png" and image.is_file()
        assert inventory["image_url"] == f"/api/image/{inventory['capture']}"
        assert inventory["cups"][0]["pixel"] == {"u": 3.0, "v": 3.0}
        assert inventory["cups"][0]["bbox_xyxy"] == [2, 2, 5, 5]
        observation = inventory["placement_observation"]
        assert observation["frame_count"] == 1
        assert observation["slots"][0]["polygon_px"] == [[1, 1], [6, 1], [6, 5], [1, 5]]
        assert rig.camera.calls == 5 and len(rig.worker.calls) == 1
        assert rig.grid.path.read_bytes() == original_grid
        controller.submit("control_preview", {"request_id": "offline_native_home_01",
                                              "action": "home", "arm": "both"})
        controller._worker.submit(lambda: None).result(timeout=3)
        assert controller.state()["robot_controls"]["proposal"]["action"] == "home"
        assert len(rig.worker.calls) == 1  # A Home proposal needs neither inference nor motion.
    finally:
        controller.close()
    rig.worker.close.assert_called_once()
