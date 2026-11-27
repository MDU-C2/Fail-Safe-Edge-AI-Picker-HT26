"""Offline raw-capture freshness, evidence and persistence checks for the CLI."""
import copy
import csv
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib
import signal
import sys
from unittest.mock import Mock

import pytest

from lynx.capture import Calibration, CaptureService, CaptureStore
from camera_client import CameraError, NoNewCameraFrame
from cup_inventory import SelectionError
from placing_grid import PlacingGrid
from table_reference_contract import _identity


@pytest.mark.parametrize("extra", [[], ["--live"], ["--live", "--standby"],
                                   ["--scan-once"], ["--live", "--coordinator-url", "http://offline.invalid"]])
def test_armed_cli_rejects_missing_or_incompatible_configuration(extra):
    from lynx.picker import arguments
    with pytest.raises(SystemExit):
        arguments(["--state-dir", "/offline/state", "--model", "/offline/model", "--armed", *extra])


def test_armed_cli_accepts_complete_configuration_and_keeps_standby_distinct():
    from lynx.picker import arguments
    args = arguments(["--state-dir", "/offline/state", "--model", "/offline/model", "--armed", "--live",
                      "--coordinator-url", "http://offline.invalid", "--coordinator-token-file", "/offline/secret"])
    assert args.armed and not args.standby
    with pytest.raises(SystemExit):
        arguments(["--state-dir", "/offline/state", "--model", "/offline/model",
                   "--coordinator-url", "http://offline.invalid"])


@pytest.fixture
def armed_launcher(tmp_path, monkeypatch):
    from lynx import picker
    from lynx.runtime_lock import NativeRuntimeLock
    state = tmp_path / "state"
    state.mkdir()
    rig = SimpleNamespace(state=state, events=[], robot=Mock(dry_run=False), capture=Mock(),
                          worker=Mock(), runner=Mock(), sessions=[], credentials=[])
    monkeypatch.setattr(picker.Model, "load", lambda _path: SimpleNamespace(precision="int8", input_size=640))
    monkeypatch.setattr(picker, "Calibration", lambda _: SimpleNamespace(digest="cal", offset=(0, 0, 0)))
    monkeypatch.setattr(picker, "PlacingGrid", lambda _: object())
    monkeypatch.setattr(picker, "CameraClient", lambda *_a, **_k: object())
    monkeypatch.setattr(picker, "NativePerception", lambda *_a, **_k: (rig.events.append("model_ready") or rig.worker))
    monkeypatch.setattr(picker, "QueueRobotClient", lambda *_a, **_k: rig.robot)
    monkeypatch.setattr(picker, "CaptureService", lambda *_a, **_k: SimpleNamespace(capture=rig.capture))
    monkeypatch.setattr(picker, "read_token", lambda path: (rig.credentials.append(path) or "private-offline"))
    monkeypatch.setattr(picker, "terminal_output", Mock())
    advice = lambda *_a, **_k: SimpleNamespace(summary=lambda *_a: "offline")
    for name in ("advise_capture_cache", "advise_packaged_native_cache", "advise_loaded_python_cache", "wait_for_model_headroom"):
        monkeypatch.setattr(picker, name, advice)
    monkeypatch.setattr(picker, "trim_unused_heap", lambda: "offline")
    def session(capture, save, robot, calibration, **kwargs):
        value = SimpleNamespace(capture=capture, robot=robot, settings=kwargs)
        rig.sessions.append(value)
        return value
    monkeypatch.setattr(picker, "CupSession", session)
    coordinator = Mock()
    monkeypatch.setitem(sys.modules, "failover_coordinator", SimpleNamespace(CoordinatorClient=coordinator))
    def runner(session, coordinator, **kwargs):
        rig.events.append("runner_ready")
        assert rig.events[0] == "model_ready"
        rig.capture.assert_not_called()
        rig.robot.execute_jobs.assert_not_called()
        session.failover = rig.runner
        rig.runner.executing = False
        rig.runner.options = kwargs
        return rig.runner
    monkeypatch.setitem(sys.modules, "failover_workflow", SimpleNamespace(FailoverRunner=runner))
    rig.run = lambda: picker.main(["--state-dir", str(state), "--model", "/offline/model", "--live", "--armed",
        "--coordinator-url", "http://coordinator.invalid", "--coordinator-token-file", str(state / "coordinator-token")])
    rig.assert_unlocked = lambda: NativeRuntimeLock(state).acquire().close()
    return rig


def test_native_armed_worker_waits_without_capture_and_releases_model_and_lease(armed_launcher):
    from handover_plan import right_camera_park_pose
    rig = armed_launcher
    assert rig.run() == 0
    assert rig.credentials == [rig.state / "tertiary-token", rig.state / "coordinator-token"]
    settings = rig.sessions[0].settings
    assert settings["between_cup_check"] and settings["between_cup_grid_check"]
    assert settings["continuous_batch"] is False and settings["right_park_pose"] == right_camera_park_pose()
    assert rig.runner.options["role"] == "tertiary"
    assert rig.runner.options["state_dir"] == rig.state
    rig.runner.serve.assert_called_once()
    rig.capture.assert_not_called()
    rig.robot.request_stop.assert_not_called()
    rig.robot.cancel_wait.assert_called_once()
    rig.robot.end_standby.assert_called_once()
    rig.worker.close.assert_called_once()
    rig.assert_unlocked()


@pytest.mark.parametrize("interrupt", ["keyboard", "sigterm", "error"])
def test_native_armed_shutdown_does_not_stop_or_cancel_gateway_queue(armed_launcher, interrupt):
    rig = armed_launcher
    def serve(*, stop_event):
        if interrupt == "keyboard":
            raise KeyboardInterrupt
        if interrupt == "error":
            raise RuntimeError("offline failure")
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert stop_event.is_set()
    rig.runner.serve.side_effect = serve
    assert rig.run() == (1 if interrupt == "error" else 0)
    rig.robot.request_stop.assert_not_called()
    rig.robot._cancel_running_queue.assert_not_called()
    rig.robot.end_standby.assert_called_once()
    rig.worker.close.assert_called_once()
    rig.assert_unlocked()


def test_native_armed_runtime_lock_refuses_duplicate_before_model_load(armed_launcher):
    from lynx.runtime_lock import NativeRuntimeLock
    rig = armed_launcher
    with NativeRuntimeLock(rig.state):
        assert rig.run() == 1
    assert rig.events == []
    rig.runner.serve.assert_not_called()


@pytest.fixture
def python_armed_launcher(tmp_path, monkeypatch):
    import failover_worker as worker
    state = tmp_path / "state"
    state.mkdir()
    (state / "secondary-token").write_text("offline-secondary")
    (state / "coordinator-token").write_text("offline-coordinator")
    (state / "placing-grid.json").write_text('{"untouched":true}')
    rig = SimpleNamespace(state=state, robot=Mock(), model=Mock(), runner=Mock(), events=[])
    def robot(api, headers, *, dry_run):
        assert api == "http://gateway.invalid/api/v1" and not dry_run
        assert headers == {"Authorization": "Bearer offline-secondary"}
        return rig.robot
    monkeypatch.setattr(worker, "QueueRobotClient", robot)
    def build(args, robot, resources):
        rig.events.append("model_ready")
        resources.callback(rig.model.close)
        return SimpleNamespace(robot=robot)
    monkeypatch.setattr(worker, "build_session", build)
    def runner(session, coordinator, **kwargs):
        assert rig.events == ["model_ready"]
        assert kwargs["role"] == "secondary" and kwargs["state_dir"] == state
        rig.events.append("armed")
        return rig.runner
    monkeypatch.setitem(sys.modules, "failover_workflow", SimpleNamespace(FailoverRunner=runner))
    coordinator = Mock()
    monkeypatch.setitem(sys.modules, "failover_coordinator", SimpleNamespace(CoordinatorClient=coordinator))
    monkeypatch.setattr(worker, "output", Mock())
    rig.run = lambda: worker.main(["--state-dir", str(state), "--model", "/offline/model.pt", "--live",
        "--gateway", "http://gateway.invalid", "--coordinator-url", "http://coordinator.invalid",
        "--coordinator-token-file", str(state / "coordinator-token")])
    return rig


@pytest.mark.parametrize("interruption", [None, "signal", "keyboard", "error"])
def test_python_armed_worker_warms_model_and_tears_down_without_robot_stop(python_armed_launcher, interruption):
    from lynx.runtime_lock import NativeRuntimeLock
    rig = python_armed_launcher
    def serve(*, stop_event):
        if interruption == "signal":
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            assert stop_event.is_set()
        elif interruption == "keyboard":
            raise KeyboardInterrupt
        elif interruption == "error":
            raise RuntimeError("offline failure")
    rig.runner.serve.side_effect = serve
    assert rig.run() == (1 if interruption == "error" else 0)
    rig.model.close.assert_called_once()
    rig.robot.end_standby.assert_called_once()
    rig.robot.request_stop.assert_not_called()
    rig.robot.execute_jobs.assert_not_called()
    assert (rig.state / "placing-grid.json").read_text() == '{"untouched":true}'
    with NativeRuntimeLock(rig.state):
        pass


def test_python_armed_worker_refuses_duplicate_before_model_load(python_armed_launcher):
    from lynx.runtime_lock import NativeRuntimeLock
    rig = python_armed_launcher
    with NativeRuntimeLock(rig.state):
        assert rig.run() == 1
    assert not rig.events
    rig.runner.serve.assert_not_called()


def test_python_worker_requires_live_and_valid_confidence_and_speed():
    from failover_worker import arguments
    base = ["--state-dir", "/offline", "--model", "/offline/model.pt", "--gateway", "http://offline.invalid",
            "--coordinator-url", "http://offline.invalid", "--coordinator-token-file", "/offline/token"]
    for extra in ([], ["--live", "--conf", "1"], ["--live", "--speed", "1501"]):
        with pytest.raises(SystemExit):
            arguments([*base, *extra])
    assert arguments([*base, "--live"]).depth_frames == 5


def test_python_worker_reuses_shared_finite_capture_backend_and_checks_lease_only_when_executing(tmp_path, monkeypatch):
    from contextlib import ExitStack
    import cup_commands
    import inference_integration
    import failover_worker
    import camera_client
    import placing_grid
    from handover_plan import right_camera_park_pose
    state = tmp_path
    store = SimpleNamespace(folder=state / "captures", save=Mock())
    store.folder.mkdir()
    camera = SimpleNamespace(intrinsics=Mock(return_value={"offline": True}))
    captures = SimpleNamespace(capture=Mock(return_value=({"capture": "fresh"}, 1.0)))
    monkeypatch.setattr(camera_client, "CameraClient", lambda *_a: camera)
    monkeypatch.setattr(placing_grid, "PlacingGrid", lambda path: SimpleNamespace(path=path))
    factory = Mock(return_value=captures)
    monkeypatch.setitem(sys.modules, "picker_workflow", SimpleNamespace(
        Calibration=lambda _: SimpleNamespace(digest="cal", offset=(1, 2, 3)),
        CaptureStore=lambda _: store, CaptureService=factory))
    model = Mock()
    def create_detector(args, calibration, intrinsics, *, register_resource):
        assert args.isolated_inference and args.mac_inference_url is None
        assert intrinsics == {"offline": True}
        register_resource(model)
        return object(), Path("/offline/model.pt")
    monkeypatch.setattr(inference_integration, "create_detector", create_detector)
    def session(capture, save, robot, calibration, **kwargs):
        return SimpleNamespace(capture=capture, settings=kwargs, robot=robot)
    monkeypatch.setattr(cup_commands, "CupSession", session)
    robot = Mock(api="http://offline.invalid/api/v1", headers={})
    args = SimpleNamespace(state_dir=state, model=Path("/offline/model.pt"), conf=0.5, threads=3, depth_frames=5, speed=100)
    with ExitStack() as resources:
        result = failover_worker.build_session(args, robot, resources)
        captures.capture.assert_not_called()
        robot.check_lease.assert_not_called()
        assert result.settings["between_cup_check"] and result.settings["between_cup_grid_check"]
        assert result.settings["continuous_batch"] is False and result.settings["right_park_pose"] == right_camera_park_pose()
        assert factory.call_args.kwargs["depth_frames"] == 5
        result.capture("scan")
        robot.check_lease.assert_not_called()
        result.failover = SimpleNamespace(executing=True)
        result.capture("between_cups_grid")
        assert robot.check_lease.call_count == 2
    model.close.assert_called_once()


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now


class RawCamera:
    api = "http://offline.invalid/api/v1"

    def __init__(self, clock):
        self.clock = clock
        self.K = {"width": 8, "height": 6, "fx": 100.0, "fy": 100.0,
                  "cx": 4.0, "cy": 3.0}
        self.calls, self.timeouts, self.raw_frames = 0, [], []
        self.sequences = None
        self.on_capture = lambda camera, frame: None

    def intrinsics(self):
        return copy.deepcopy(self.K)

    def capture_raw(self, timeout_s):
        self.calls += 1
        self.timeouts.append(timeout_s)
        started = self.clock.now
        self.clock.now += 0.1
        sequence = self.sequences[self.calls - 1] if self.sequences else self.calls
        frame = {"width": 8, "height": 6, "sequence": sequence,
                 "timestamp": f"2026-11-13T12:00:{self.calls:02d}+00:00",
                 "observed_at": f"2026-11-13T12:00:{self.calls:02d}+00:00",
                 "observation_started_monotonic": started,
                 "received_monotonic": self.clock.now}
        rgb = bytes([self.calls]) * (8 * 6 * 3)
        depth = struct.pack("<H", 500 + self.calls) * (8 * 6)
        self.raw_frames.append((rgb, depth, copy.deepcopy(frame)))
        self.on_capture(self, frame)
        return rgb, depth, frame


class ConsumingWorker:
    def __init__(self):
        self.model = SimpleNamespace(names=("CUP", "CUP BASE", "CUP HANDLE", "TRAY"),
                                     weights=Path("/offline/model.ncnn.bin"),
                                     sha256="b" * 64, precision="int8")
        self.calls = []
        self.on_observe = lambda: None

    def observe(self, rgb, depths, config):
        pixels = config["intrinsics"]["width"] * config["intrinsics"]["height"]
        assert isinstance(rgb, (bytes, bytearray)) and len(rgb) == pixels * 3
        assert all(isinstance(raw, (bytes, bytearray)) and len(raw) == pixels * 2 for raw in depths)
        record = {"rgb": rgb, "depths": tuple(depths), "list": depths,
                  "config": copy.deepcopy(config)}
        self.calls.append(record)
        values = [struct.unpack_from("<H", raw)[0] for raw in depths]
        depths.clear()  # Match NativePerception's ownership/streaming contract.
        mean = sum(values) / len(values)
        obj = {"class": "CUP BASE", "detection_id": 0, "confidence": 0.9,
               "x_px": 3.0, "y_px": 3.0, "bbox_xyxy": [2, 2, 5, 5],
               "x_mm": 0.0, "y_mm": 0.0, "z_mm": mean,
               "robot_x_mm": 300.0, "robot_y_mm": 100.0, "robot_z_mm": 700 - mean,
               "mask_area_px": 9, "diameter_mm": 50.0, "cup_height": True,
               "depth_quality": {"valid": True, "reason": None}}
        if len(values) > 1:
            obj["robot_z_frames_mm"] = [700 - value for value in values]
        slots = [{"id": place["id"], "state": "clear", "reason": "Clear visible table.",
                  "reason_code": "clear", "polygon_px": [[1, 1], [6, 1], [6, 5], [1, 5]],
                  "valid_fraction": 1.0, "table_fraction": 1.0,
                  "visible_fraction": 1.0, "floor_visible_fraction": 1.0,
                  "assigned_cups": [], "cup_count": 0, "evidence": []}
                 for place in config["grid_snapshot"]["locations"]]
        self.on_observe()
        return {"objects": [obj], "slots": slots, "inference_ms": 2.0}


@pytest.fixture
def capture_rig(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("lynx.capture.time.monotonic", clock.monotonic)
    camera, worker = RawCamera(clock), ConsumingWorker()
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps({"scale": 1.0,
        "R": [[1, 0, 0], [0, 1, 0], [0, 0, -1]], "t": [0, 0, 700], "tag_offset": [0, 0, 0]}))
    calibration = Calibration(calibration_path)
    grid_path = tmp_path / "placing-grid.json"
    grid_path.write_text(json.dumps({"cell_size_mm": 90, "operator_note": "preserve",
        "locations": [{"id": 2, "x": 400, "y": -200, "z": 230,
                       "occupied": True, "reserved": True, "reservation_id": "existing-batch",
                       "vision_blocked": True},
                      {"id": 3, "x": 500, "y": -200, "z": 230,
                       "occupied": True, "vision_blocked": True}]}))
    grid = PlacingGrid(grid_path)
    reference_path = grid_path.with_suffix(".table-reference.json")
    reference = {"schema_version": 1, "kind": "placement_table_reference", "units": "mm",
                 "identity": _identity(calibration, camera.K, grid.snapshot()["locations"], 90, 0),
                 "plane_z_mm": [0.0, 0.0, -10.0],
                 "source": {"capture": "independent-empty-table", "depth_sha256": "a" * 64,
                            "frame_sequences": [10, 11, 12]},
                 "fit": {"sample_count": 100, "bin_count": 100, "inlier_bin_count": 100,
                         "rms_mm": 1.0, "max_cell_residual_p99_mm": 2.0}}
    reference_path.write_text(json.dumps(reference))
    store = CaptureStore(tmp_path / "captures")
    service = CaptureService(camera, worker, calibration, grid, store, depth_frames=5)
    return SimpleNamespace(clock=clock, camera=camera, worker=worker, calibration=calibration,
                           grid=grid, store=store, service=service, reference_path=reference_path)


def depth_png_samples(path):
    """Decode the writer's unfiltered small 16-bit PNG using only stdlib."""
    raw = Path(path).read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    offset, payload, width, height = 8, bytearray(), None, None
    while offset < len(raw):
        size = struct.unpack_from(">I", raw, offset)[0]
        kind, data = raw[offset + 4:offset + 8], raw[offset + 8:offset + 8 + size]
        if kind == b"IHDR":
            width, height, bits, color, *_ = struct.unpack(">IIBBBBB", data)
            assert (bits, color) == (16, 0)
        elif kind == b"IDAT":
            payload.extend(data)
        offset += size + 12
    decoded = zlib.decompress(payload)
    rows = [decoded[index * (1 + width * 2):(index + 1) * (1 + width * 2)] for index in range(height)]
    assert all(row[0] == 0 for row in rows)
    return [value for row in rows for value in struct.unpack(">" + "H" * width, row[1:])]


def test_five_samples_consumed_but_audit_depth_is_newest_not_mean(capture_rig):
    rig = capture_rig
    inventory, started = rig.service.capture("verification")
    call = rig.worker.calls[0]
    assert [struct.unpack_from("<H", raw)[0] for raw in call["depths"]] == [501, 502, 503, 504, 505]
    assert call["list"] == []
    assert call["rgb"] == rig.camera.raw_frames[-1][0]
    assert depth_png_samples(inventory["images"]["depth"]) == [505] * 48
    record = json.loads((rig.store.folder / f"detections_{inventory['capture']}.json").read_text())
    assert record["objects"][0]["z_mm"] == 503  # Native object means stay distinct from raw grid depth.
    assert inventory["cups"][0]["z_frames"] == [199, 198, 197, 196, 195]
    assert started == 100.0
    assert inventory["frame"]["sequence"] == 5
    assert "observation_started_monotonic" not in inventory["frame"]
    assert inventory["frame"]["depth_sampling"]["sequences"] == [1, 2, 3, 4, 5]
    assert inventory["frame"]["depth_sampling"]["grid_evidence"] == "latest_raw_aligned_frame"
    assert all(0 < duration <= 10 for duration in rig.camera.timeouts)
    assert rig.camera.timeouts == sorted(rig.camera.timeouts, reverse=True)


def test_scan_uses_three_distinct_bursts_and_preserves_occupied_reserved_grid(capture_rig):
    rig = capture_rig
    rig.service = CaptureService(rig.camera, rig.worker, rig.calibration, rig.grid,
                                 rig.store, scan_observations=3)
    original_grid = rig.grid.path.read_bytes()
    inventory, started = rig.service.capture("scan")
    assert rig.camera.calls == 15 and len(rig.worker.calls) == 3
    observation = inventory["placement_observation"]
    assert observation["frame_count"] == 3
    assert observation["frame_sequences"] == [5, 10, 15]
    assert observation["applied"] is False and observation["released_ids"] == []
    assert all(slot["state"] == "clear" for slot in observation["slots"])
    assert started == pytest.approx(101.0)  # Oldest sample of the final observation, not its newest.
    assert depth_png_samples(inventory["images"]["depth"]) == [515] * 48
    assert rig.grid.path.read_bytes() == original_grid
    assert observation["grid_snapshot"]["locations"] == rig.grid.snapshot()["locations"]
    assert json.loads((rig.store.folder / "cups.json").read_text())["capture"] == inventory["capture"]


def test_default_scan_uses_one_inference_and_truthfully_records_five_depth_samples(capture_rig):
    rig = capture_rig
    original_grid = rig.grid.path.read_bytes()
    snapshot = rig.grid.snapshot()
    inventory, started = rig.service.capture("scan")
    assert rig.camera.calls == 5 and len(rig.worker.calls) == 1
    observation = inventory["placement_observation"]
    assert observation["frame_count"] == 1
    assert observation["frame_sequences"] == [5]
    assert observation["message"] == "Placement area checked against one fresh depth observation."
    assert observation["grid_snapshot"] == snapshot
    assert observation["applied"] is False and observation["released_ids"] == []
    assert observation["cup_summary"] == {"assigned_count": 0, "location_ids": [],
        "min_assigned_count": 0, "max_assigned_count": 0, "stable_cell_counts": True}
    assert inventory["frame"]["depth_sampling"]["count"] == 5
    assert len(inventory["model"]["observation_results"]) == 1
    assert started == 100.0
    assert depth_png_samples(inventory["images"]["depth"]) == [505] * 48
    assert rig.grid.path.read_bytes() == original_grid  # Includes occupied and reserved rows.
    saved = json.loads((rig.store.folder / f"detections_{inventory['capture']}.json").read_text())
    assert saved["placement_observation"] == observation


@pytest.mark.parametrize("count", [True, False, 0, 2, 4, 1.0, "3", None])
def test_scan_observation_count_requires_exact_supported_integer(capture_rig, count):
    rig = capture_rig
    with pytest.raises(ValueError, match="Scan observation count must be 1 or 3"):
        CaptureService(rig.camera, rig.worker, rig.calibration, rig.grid, rig.store,
                       scan_observations=count)
    assert not rig.worker.calls and rig.camera.calls == 0


def test_three_observation_scan_option_does_not_repeat_verification(capture_rig):
    rig = capture_rig
    service = CaptureService(rig.camera, rig.worker, rig.calibration, rig.grid,
                             rig.store, scan_observations=3)
    inventory, _ = service.capture("verification")
    assert rig.camera.calls == 5 and len(rig.worker.calls) == 1
    assert inventory["placement_observation"]["frame_count"] == 1
    assert "grid_snapshot" not in inventory["placement_observation"]


@pytest.mark.parametrize("malformed", ["missing_fraction", "invalid_evidence"])
def test_malformed_single_scan_is_not_saved_or_applied(capture_rig, monkeypatch, malformed):
    rig = capture_rig
    original_grid = rig.grid.path.read_bytes()
    observe = rig.worker.observe
    def malformed_observation(*args):
        result = observe(*args)
        if malformed == "missing_fraction":
            result["slots"][0].pop("valid_fraction")
        else:
            result["slots"][0]["evidence"] = 123
        return result
    monkeypatch.setattr(rig.worker, "observe", malformed_observation)
    with pytest.raises(SelectionError, match="Placement evidence"):
        rig.service.capture("scan")
    assert len(rig.worker.calls) == 1
    assert not list(rig.store.folder.iterdir())
    assert rig.grid.path.read_bytes() == original_grid


@pytest.mark.parametrize("sequences", [[1, 1, 2, 3, 4], [5, 4, 6, 7, 8], [True, 2, 3, 4, 5]])
def test_burst_uses_available_distinct_sequences_without_restart_gate(capture_rig, sequences):
    rig = capture_rig
    rig.camera.sequences = sequences
    if sequences[0] is True:
        with pytest.raises(CameraError, match="sequence"):
            rig.service.capture("verification")
        assert not rig.worker.calls
        assert not list(rig.store.folder.iterdir())
    else:
        inventory, _ = rig.service.capture("verification")
        expected = 1 if sequences[0] == sequences[1] else 5
        assert len(rig.worker.calls) == 1
        assert len(rig.worker.calls[0]["depths"]) == expected


@pytest.mark.parametrize("second", [5, 4])
def test_scan_rejects_nonadvancing_observations_even_if_each_burst_advances(capture_rig, second):
    rig = capture_rig
    rig.service = CaptureService(rig.camera, rig.worker, rig.calibration, rig.grid,
                                 rig.store, scan_observations=3)
    rig.camera.sequences = [1, 2, 3, 4, 5] + list(range(second - 4, second + 1))
    with pytest.raises(CameraError, match="advancing camera observations"):
        rig.service.capture("scan")
    assert len(rig.worker.calls) == 2
    assert not list(rig.store.folder.iterdir())


def test_frame_dimensions_change_aborts_before_native_inference(capture_rig):
    rig = capture_rig
    rig.camera.on_capture = lambda camera, frame: frame.update(width=9) if camera.calls == 3 else None
    with pytest.raises(CameraError, match="dimensions"):
        rig.service.capture("verification")
    assert not rig.worker.calls
    assert not list(rig.store.folder.iterdir())


def test_native_burst_uses_latest_first_pair_and_available_samples(capture_rig):
    rig = capture_rig
    capture = rig.camera.capture_raw
    rig.camera.capture_latest_raw = capture
    def no_new_frame(timeout_s):
        raise NoNewCameraFrame("Camera did not publish another frame")
    rig.camera.capture_raw = no_new_frame
    inventory, _ = rig.service.capture("scan")
    assert rig.camera.calls == 1
    assert inventory["frame"]["depth_sampling"]["count"] == 1
    assert len(rig.worker.calls[0]["depths"]) == 1


def test_native_burst_does_not_swallow_transport_errors_after_first_sample(capture_rig):
    rig = capture_rig
    rig.camera.capture_latest_raw = rig.camera.capture_raw
    def disconnected(timeout_s):
        raise CameraError("Gateway disconnected")
    rig.camera.capture_raw = disconnected
    with pytest.raises(CameraError, match="disconnected"):
        rig.service.capture("scan")
    assert not rig.worker.calls


@pytest.mark.parametrize("observations", [1, 3])
def test_between_cup_grid_capture_uses_fresh_bursts_and_complete_evidence(capture_rig, observations):
    rig = capture_rig
    original_grid = rig.grid.path.read_bytes()
    latest = rig.store.folder / "cups.json"
    latest.write_text('{"capture":"active-batch","cups":[]}')
    before_latest = latest.read_bytes()
    def refuse_cached_pair(timeout_s):
        raise AssertionError("Between-cup capture must wait for a new camera frame")
    rig.camera.capture_latest_raw = refuse_cached_pair
    service = CaptureService(rig.camera, rig.worker, rig.calibration, rig.grid,
                             rig.store, scan_observations=observations)
    inventory, _ = service.capture("between_cups_grid")
    evidence = inventory["placement_observation"]
    assert rig.camera.calls == 5 * observations
    assert len(rig.worker.calls) == observations
    assert evidence["frame_sequences"] == [5 * (index + 1) for index in range(observations)]
    assert evidence["frame_count"] == observations
    assert evidence["grid_snapshot"] == rig.grid.snapshot()
    assert evidence["applied"] is False
    assert rig.grid.path.read_bytes() == original_grid
    assert latest.read_bytes() == before_latest
    saved = json.loads((rig.store.folder / f"detections_{inventory['capture']}.json").read_text())
    assert saved["placement_observation"] == evidence


@pytest.mark.parametrize("purpose", ["between_cups", "between_cups_grid"])
def test_between_cup_capture_rejects_frozen_camera_without_using_cached_pair(capture_rig, purpose):
    rig = capture_rig
    cached = rig.camera.capture_raw
    rig.camera.capture_latest_raw = cached
    def no_new_frame(timeout_s):
        raise NoNewCameraFrame("Camera frozen after placement")
    rig.camera.capture_raw = no_new_frame
    with pytest.raises(NoNewCameraFrame, match="frozen"):
        rig.service.capture(purpose)
    assert rig.camera.calls == 0 and not rig.worker.calls
    assert not list(rig.store.folder.iterdir())


@pytest.mark.parametrize("coordinates,assigned,expected", [
    ((300.0, 100.0, 70.0), False, 1),  # Visible source cup without a base.
    ((400.0, -200.0, 70.0), False, 0),  # Cup located inside a grid cell.
    ((None, None, None), False, 1),  # Unlocated cup cannot prove an empty workspace.
    ((400.0, -200.0, None), False, 1),  # Missing depth prevents physical exclusion.
    ((None, None, None), True, 0),  # Latest native assignment identifies a placed cup.
])
def test_native_source_presence_counts_missing_base_cups_outside_current_grid(
        capture_rig, monkeypatch, coordinates, assigned, expected):
    rig = capture_rig
    observe = rig.worker.observe
    def cups_without_bases(*args):
        result = observe(*args)
        result["objects"] = [{"class": "CUP", "detection_id": 10, "confidence": 0.9,
            "x_px": 3.0, "y_px": 3.0, "bbox_xyxy": [1, 1, 6, 5],
            **dict(zip(("robot_x_mm", "robot_y_mm", "robot_z_mm"), coordinates))}]
        if assigned:
            result["slots"][0]["assigned_cups"] = [{"object_indices": [0], "detection_ids": [10]}]
        return result
    monkeypatch.setattr(rig.worker, "observe", cups_without_bases)
    inventory, _ = rig.service.capture("between_cups_grid")
    assert inventory["cups"] == []
    assert inventory["source_presence"] == {"cup_detections": expected}


def test_source_presence_uses_latest_assignment_not_an_older_grid_observation(capture_rig, monkeypatch):
    rig = capture_rig
    service = CaptureService(rig.camera, rig.worker, rig.calibration, rig.grid,
                             rig.store, scan_observations=3)
    observe = rig.worker.observe
    def moving_cup(*args):
        result = observe(*args)
        result["objects"] = [{"class": "CUP", "detection_id": 10, "confidence": 0.9,
            "x_px": 3.0, "y_px": 3.0, "bbox_xyxy": [1, 1, 6, 5],
            "robot_x_mm": None, "robot_y_mm": None, "robot_z_mm": None}]
        if len(rig.worker.calls) < 3:
            result["slots"][0]["assigned_cups"] = [{"object_indices": [0], "detection_ids": [10]}]
        return result
    monkeypatch.setattr(rig.worker, "observe", moving_cup)
    inventory, _ = service.capture("between_cups_grid")
    assert inventory["source_presence"] == {"cup_detections": 1}
    assert inventory["placement_observation"]["cup_summary"]["assigned_count"] == 0


@pytest.mark.parametrize("when", ["before", "during_burst", "during_inference"])
def test_capture_uses_loaded_intrinsics_without_repeated_source_checks(capture_rig, when):
    rig = capture_rig
    def change():
        rig.camera.K["fx"] += 1
    if when == "before":
        change()
    elif when == "during_burst":
        rig.camera.on_capture = lambda camera, frame: change() if camera.calls == 3 else None
    else:
        rig.worker.on_observe = change
    inventory, _ = rig.service.capture("verification")
    assert inventory["frame"]["sequence"] == 5
    assert len(rig.worker.calls) == 1
    assert rig.worker.calls[0]["config"]["intrinsics"]["fx"] == 100.0


@pytest.mark.parametrize("changed", ["reference", "grid_geometry"])
@pytest.mark.parametrize("when", ["during_burst", "during_inference"])
def test_changed_capture_identity_aborts_without_audit_or_grid_reconciliation(capture_rig, changed, when):
    rig = capture_rig
    original_grid = rig.grid.path.read_bytes()
    def change():
        if changed == "grid_geometry":
            value = json.loads(original_grid)
            value["locations"][0]["x"] += 1
            rig.grid.path.write_text(json.dumps(value))
        else:
            path = rig.calibration.path if changed == "calibration" else rig.reference_path
            path.write_bytes(path.read_bytes() + b"\n")
    if when == "during_burst":
        rig.camera.on_capture = lambda camera, frame: change() if camera.calls == 3 else None
    else:
        rig.worker.on_observe = change
    with pytest.raises(SelectionError, match="changed"):
        rig.service.capture("verification")
    assert not list(rig.store.folder.iterdir())
    if changed != "grid_geometry":
        assert rig.grid.path.read_bytes() == original_grid


def test_optional_gripper_csv_deduplicates_and_keeps_final_uncertain_correction(capture_rig):
    rig = capture_rig
    inventory, _ = rig.service.capture("verification")
    cup = inventory["cups"][0]
    cup.update(status="placed", execution={"action": "goto"}, grip_left_mm=12.3, grip_right_mm=11.8,
               last_verification={"capture": "fresh-confirmed", "grip_geometry": {"diameter": 50.0}})
    rig.store.save(inventory)
    rig.store.save(inventory)
    cup["status"] = "movement_unconfirmed"
    rig.store.save(inventory)
    with (rig.store.folder / "grips.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["status"] for row in rows] == ["placed", "movement_unconfirmed"]
    assert all(row["diameter_mm"] == "50.0" and row["grip_left_mm"] == "12.3"
               and row["grip_right_mm"] == "11.8" for row in rows)
    assert json.loads((rig.store.folder / "cups.json").read_text())["cups"][0]["status"] == "movement_unconfirmed"


def test_verification_saves_evidence_without_replacing_active_inventory(capture_rig):
    rig = capture_rig
    latest = rig.store.folder / "cups.json"
    original = b'{"capture":"active-batch","cups":[{"id":1,"status":"movement_unconfirmed"}]}\n'
    latest.write_bytes(original)
    inventory, _ = rig.service.capture("verification")
    assert latest.read_bytes() == original
    assert (rig.store.folder / f"cups_{inventory['capture']}.json").exists()
    assert (rig.store.folder / f"detections_{inventory['capture']}.json").exists()


def test_optional_csv_error_does_not_erase_authoritative_recovery_record(capture_rig, monkeypatch):
    rig = capture_rig
    inventory, _ = rig.service.capture("verification")
    inventory["cups"][0].update(status="movement_unconfirmed", execution={"action": "goto"})
    original_open = Path.open
    def failing_csv(path, *args, **kwargs):
        if path.name == "grips.csv":
            raise PermissionError("offline telemetry failure")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failing_csv)
    rig.store.save(inventory)
    assert json.loads((rig.store.folder / "cups.json").read_text())["cups"][0]["status"] == "movement_unconfirmed"


# Raw HTTP frame boundaries; mocked streams allocate at most a few bytes.
import unittest

from camera_client import CameraClient


class RawResponse:
    def __init__(self, name, *, headers=None, chunks=(), data=None):
        self.name, self.headers, self.chunks, self.data = name, headers or {}, chunks, data
        self.closed = False
        self.body_reads = 0
        self.chunks_read = 0

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        self.body_reads += 1
        for chunk in self.chunks:
            self.chunks_read += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class RawSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers, stream, timeout):
        self.calls.append((url, stream))
        if not self.responses:
            raise AssertionError("Invalid frame should fail without retrying or making a network request")
        response = self.responses.pop(0)
        assert url.endswith("/camera/" + response.name)
        return response


def raw_fixture(*, width=2, height=1, rgb_chunks=(b"123456",), depth_chunks=(b"1234",),
                rgb_headers=None, depth_headers=None, max_dimensions=(2, 1)):
    common = {
        "X-Frame-Sequence": "11",
        "X-Frame-Width": str(width),
        "X-Frame-Height": str(height),
        "X-Frame-Timestamp": "2026-11-13T08:00:00+00:00",
    }
    status = RawResponse("status", data={"connected": True, "sequence": 10})
    ready = RawResponse("status", data={"connected": True, "sequence": 11})
    rgb = RawResponse("rgb", headers={**common, "X-Pixel-Format": "BGR888p", **(rgb_headers or {})},
                      chunks=rgb_chunks)
    depth = RawResponse("depth", headers={**common, "X-Pixel-Format": "RAW16", **(depth_headers or {})},
                        chunks=depth_chunks)
    session = RawSession([status, ready, rgb, depth])
    client = CameraClient("http://offline.invalid/api/v1", {}, session=session,
                          max_dimensions=max_dimensions)
    return client, session, (status, ready, rgb, depth)


class RawCameraBoundaryTests(unittest.TestCase):
    def assert_closed_without_retry(self, session, responses):
        self.assertEqual(len(session.calls), 4)
        self.assertTrue(all(response.closed for response in responses))

    def test_configured_dimensions_accept_exact_boundary_and_reject_each_axis_before_body(self):
        client, session, responses = raw_fixture()
        rgb, depth, metadata = client.capture_raw()
        self.assertEqual(rgb, b"123456")
        self.assertEqual(depth, b"1234")
        self.assertEqual((metadata["width"], metadata["height"]), (2, 1))
        self.assert_closed_without_retry(session, responses)

        for width, height in ((3, 1), (2, 2)):
            with self.subTest(width=width, height=height):
                client, session, responses = raw_fixture(width=width, height=height)
                with self.assertRaisesRegex(CameraError, "configured dimension limit"):
                    client.capture_raw()
                self.assertEqual([response.body_reads for response in responses], [0, 0, 0, 0])
                self.assert_closed_without_retry(session, responses)

    def test_invalid_or_mismatched_peer_headers_fail_before_any_body(self):
        # All of these are rejected from headers, including an 8.4M+ declaration;
        # the fake response bodies remain only six and four bytes long.
        cases = [
            ("rgb", {"X-Frame-Width": "1.5"}, "Invalid frame width"),
            ("depth", {"X-Frame-Height": "0"}, "Invalid frame height"),
            ("depth", {"X-Frame-Width": "8400001"}, "8.4 megapixel"),
            ("depth", {"X-Pixel-Format": "RAW8"}, "Unsupported camera format"),
            ("depth", {"X-Frame-Timestamp": " "}, "Missing camera frame timestamp"),
            ("depth", {"X-Frame-Width": "1"}, "dimensions do not match"),
            ("depth", {"X-Frame-Timestamp": "2026-11-13T08:00:01+00:00"}, "different timestamps"),
        ]
        for peer, changed, message in cases:
            with self.subTest(peer=peer, changed=changed):
                client, session, responses = raw_fixture(**{peer + "_headers": changed})
                with self.assertRaisesRegex(CameraError, message):
                    client.capture_raw()
                self.assertEqual([response.body_reads for response in responses], [0, 0, 0, 0])
                self.assert_closed_without_retry(session, responses)

    def test_overlong_chunked_payload_stops_at_first_extra_byte_and_closes_both_streams(self):
        for peer, expected in (("rgb", b"123456"), ("depth", b"1234")):
            with self.subTest(peer=peer):
                chunks = (expected, b"!", AssertionError("Must stop at first oversized chunk"))
                client, session, responses = raw_fixture(**{peer + "_chunks": chunks})
                with self.assertRaisesRegex(CameraError, "larger than its declared dimensions"):
                    client.capture_raw()
                failed = responses[2 if peer == "rgb" else 3]
                self.assertEqual(failed.chunks_read, 2)
                if peer == "rgb":
                    self.assertEqual(responses[3].body_reads, 0)
                self.assert_closed_without_retry(session, responses)

    def test_truncated_chunked_payload_is_rejected_and_closes_both_streams(self):
        for peer, chunks, length in (("rgb", (b"", b"12", b"345"), 5),
                                     ("depth", (b"1", b"", b"23"), 3)):
            with self.subTest(peer=peer):
                client, session, responses = raw_fixture(**{peer + "_chunks": chunks})
                with self.assertRaisesRegex(CameraError, f"payload has {length} bytes; expected"):
                    client.capture_raw()
                if peer == "rgb":
                    self.assertEqual(responses[3].body_reads, 0)
                self.assert_closed_without_retry(session, responses)
