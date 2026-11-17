"""CLI import, wire contract, audit images and state-preservation regressions."""
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from lynx.native_perception import NativePerception
from lynx.native_transport import InferenceError
from lynx.picker import arguments
from lynx.raw_images import write_png


def test_runtime_imports_no_image_ml_camera_or_web_libraries():
    code = '''
import importlib.abc, sys
blocked = {'numpy','cv2','torch','ultralytics','depthai','fastapi','uvicorn','gateway','starlette'}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in blocked:
            raise AssertionError('Heavy dependency imported: ' + fullname)
sys.meta_path.insert(0, Guard())
import lynx.picker
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cli_requires_explicit_live_and_rejects_live_scan_once():
    options = ["--state-dir", "/state", "--model", "/model"]
    assert arguments(options).live is False
    assert arguments(options + ["--live"]).live is True
    with pytest.raises(SystemExit):
        arguments(options + ["--live", "--scan-once"])


def test_cli_verification_age_default_override_and_invalid_values():
    options = ["--state-dir", "/state", "--model", "/model"]
    assert arguments(options).max_result_age == 60.0
    assert arguments(options + ["--max-result-age", "45"]).max_result_age == 45.0
    for value in ("0", "-1", "nan", "inf"):
        with pytest.raises(SystemExit):
            arguments(options + ["--max-result-age", value])


def test_standard_library_png_writes_exact_color_and_16bit_depth(tmp_path):
    image = np.arange(7 * 11 * 3, dtype=np.uint8).reshape(7, 11, 3)
    depth = np.array([0, 1, 255, 256, 1000, 32768, 65535] * 11, dtype="<u2").reshape(7, 11)
    color_path, depth_path = tmp_path / "rgb.png", tmp_path / "depth.png"
    write_png(color_path, image.transpose(2, 0, 1).tobytes(), 11, 7)
    write_png(depth_path, depth.tobytes(), 11, 7, depth=True)
    np.testing.assert_array_equal(cv2.imread(str(color_path)), image)
    np.testing.assert_array_equal(cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED), depth)


@pytest.fixture
def worker_script(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text('''import json,struct,sys
mode=sys.argv[1]
def send(i,value):
 data=json.dumps(value).encode()
 sys.stdout.buffer.write(struct.pack('<4sIIII',b'LNXR',i,0,len(data),0)+data)
 sys.stdout.buffer.flush()
send(0,{'ready':True,'protocol_version':1,'perception_version':2,'input_size':640,'classes_count':4,'max_detections':32})
while True:
 header=sys.stdin.buffer.read(32)
 if not header: break
 magic,i,w,h,bgr,n,size,reserved=struct.unpack('<4sIIIIIII',header)
 assert magic==b'LNXP' and n==2 and not reserved
 config=json.loads(sys.stdin.buffer.read(size))
 first=sys.stdin.buffer.read(w*h*2)
 last=sys.stdin.buffer.read(w*h*2)
 rgb=sys.stdin.buffer.read(bgr)
 assert first==bytes([1])*(w*h*2) and last==bytes([2])*(w*h*2)
 assert rgb==bytes([3])*(w*h*3)
 send(i,{'width':w+(mode=='width'),'height':h,'perception_version':2,'inference_ms':1,
         'objects':[], 'slots':[{'id':99 if mode=='grid' else 2,'state':'clear'}]})
''')
    return script


def native(worker_script, mode="ok"):
    model = SimpleNamespace(param="none", weights="none", input_size=640,
                            names=("CUP", "CUP BASE", "CUP HANDLE", "TRAY"))
    return NativePerception("unused", model, command=[sys.executable, str(worker_script), mode], timeout=1)


def test_v2_consumes_depth_burst_in_order_and_keeps_worker_reusable(worker_script):
    worker = native(worker_script)
    config = {"intrinsics": {"width": 3, "height": 2}, "grid_snapshot": {"locations": [{"id": 2}]}}
    try:
        for _ in range(2):
            depths = [bytes([1]) * 12, bytes([2]) * 12]
            result = worker.observe(bytes([3]) * 18, depths, config)
            assert depths == []
            assert result["slots"] == [{"id": 2, "state": "clear"}]
            assert worker.process.poll() is None
    finally:
        worker.close()


@pytest.mark.parametrize("mode", ["width", "grid"])
def test_v2_corrupt_geometry_discards_native_worker(worker_script, mode):
    worker = native(worker_script, mode)
    try:
        with pytest.raises(InferenceError):
            worker.observe(bytes([3])*18, [bytes([1])*12, bytes([2])*12],
                           {"intrinsics": {"width": 3, "height": 2},
                            "grid_snapshot": {"locations": [{"id": 2}]}})
        assert worker.process.poll() is not None
    finally:
        worker.close()


def test_oversized_frame_rejected_before_sending(worker_script):
    worker = native(worker_script)
    try:
        with pytest.raises(InferenceError, match="bounded"):
            worker.observe(b"", [b""], {"intrinsics": {"width": 1921, "height": 1}})
        assert worker.sequence == 0
    finally:
        worker.close()


@pytest.fixture
def offline_launcher(tmp_path, monkeypatch):
    """Real terminal/session/journal; every device and model boundary is fake."""
    from lynx import picker

    state = tmp_path / "state"
    state.mkdir()
    started, stopped, finished = threading.Event(), threading.Event(), threading.Event()
    rig = SimpleNamespace(state=state, started=started, stopped=stopped, finished=finished,
                          stop_calls=0, fail_stop=False, repeat_term=False, worker_closed=False,
                          session=None, terminal=None)

    class FakeRobot:
        def __init__(self, _api, _headers, *, dry_run, require_session):
            assert not require_session
            self.dry_run, self.stop_requested = dry_run, False

        def configure_operator_controls(self, _profile):
            pass

        def latch_stop(self):
            self.stop_requested = True

        def request_stop(self):
            rig.stop_calls += 1
            assert self.stop_requested and rig.session.stop_requested
            # The actual session must persist the lock before contacting even
            # this fake robot, including when the stop subsequently fails.
            assert rig.session.recovery.required()
            stopped.set()
            if rig.fail_stop:
                raise RuntimeError("offline stop unavailable")
            return {"stopped": True, "message": "offline stop confirmed"}

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            if rig.repeat_term:
                signal.raise_signal(signal.SIGTERM)
            rig.worker_closed = True

    def no_capture(*_args, **_kwargs):
        raise AssertionError("The termination test must not capture or send model work")

    def command_handler(session, _language, _controls):
        def command(line):
            assert line == "offline-action"
            session.recovery.begin({"action": "offline_active_command"})
            started.set()
            try:
                assert stopped.wait(5), "Shutdown did not attempt stop"
                assert session.stop_requested
            finally:
                finished.set()
            return True
        return command

    real_session, real_terminal = picker.CupSession, picker.TerminalController

    def session_factory(*args, **kwargs):
        rig.session = real_session(*args, **kwargs)
        return rig.session

    def terminal_factory(*args, **kwargs):
        rig.terminal = real_terminal(*args, **kwargs)
        return rig.terminal

    monkeypatch.setattr(picker.Model, "load", lambda _path: SimpleNamespace(precision="int8", input_size=640))
    monkeypatch.setattr(picker, "Calibration", lambda _path: SimpleNamespace(digest="a" * 64, offset=(0, 0, 0)))
    monkeypatch.setattr(picker, "PlacingGrid", lambda path: SimpleNamespace(path=path))
    monkeypatch.setattr(picker, "read_token", lambda _path: "offline-test-token")
    monkeypatch.setattr(picker, "CameraClient", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(picker, "advise_loaded_python_cache", lambda: SimpleNamespace(summary=lambda: "offline source-cache advice"))
    monkeypatch.setattr(picker, "trim_unused_heap", lambda: "offline heap trim")
    monkeypatch.setattr(picker, "wait_for_model_headroom", lambda _amount: SimpleNamespace(summary=lambda _amount: "offline memory check"))
    monkeypatch.setattr(picker, "NativePerception", FakeWorker)
    monkeypatch.setattr(picker, "CaptureService", lambda *_args, **_kwargs: SimpleNamespace(capture=no_capture))
    monkeypatch.setattr(picker, "RobotClient", FakeRobot)
    monkeypatch.setattr(picker, "CupSession", session_factory)
    monkeypatch.setattr(picker, "TerminalController", terminal_factory)
    monkeypatch.setattr(picker, "CommandHandler", command_handler)
    rig.run = lambda: picker.main(["--state-dir", str(state), "--model", "/offline/model", "--live"])
    return rig


def assert_launcher_created_no_lock(rig):
    assert not (rig.state / "picker.lock").exists()


def test_empty_enter_does_not_capture_and_launcher_selects_single_verification(offline_launcher, monkeypatch):
    rig = offline_launcher
    lines = iter(["", "   ", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(lines))
    assert rig.run() == 0
    assert rig.session.verification_frames == 1
    assert rig.session.strict_single_verification is True
    assert rig.session.max_result_age == 60.0
    assert rig.session.home_after_cup is False
    assert rig.session.motion_settings()["home_after_cup"] is False
    assert not rig.started.is_set() and rig.stop_calls == 0
    assert rig.worker_closed
    assert_launcher_created_no_lock(rig)


@pytest.mark.parametrize("fail_stop", [False, True])
def test_sigterm_active_command_attempts_stop_and_preserves_recovery(offline_launcher, monkeypatch, fail_stop):
    rig = offline_launcher
    rig.fail_stop = fail_stop
    rig.repeat_term = True  # A supervisor may repeat TERM while cleanup runs.
    calls = 0

    def input_line(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "offline-action"
        assert rig.started.wait(5)
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt the input loop")

    monkeypatch.setattr("builtins.input", input_line)
    previous = signal.getsignal(signal.SIGTERM)
    assert rig.run() == 0
    assert rig.stop_calls == 1
    assert rig.finished.is_set() and not rig.terminal.busy
    assert rig.session.motion_uncertain and rig.session.recovery.required()
    assert rig.worker_closed and signal.getsignal(signal.SIGTERM) == previous
    record = json.loads((rig.state / "captures/robot_recovery.json").read_text())
    assert record["recovery_required"] is True
    assert record["context"]["action"] == "software_stop"
    assert record["context"]["stopped"] is (not fail_stop)
    assert_launcher_created_no_lock(rig)


def test_sigterm_idle_exits_without_robot_command_and_restores_handler(offline_launcher, monkeypatch):
    rig = offline_launcher

    def input_line(_prompt):
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("SIGTERM did not interrupt the idle input loop")

    monkeypatch.setattr("builtins.input", input_line)
    previous = signal.getsignal(signal.SIGTERM)
    assert rig.run() == 0
    assert rig.stop_calls == 0 and not rig.started.is_set()
    assert not rig.session.recovery.required()
    assert rig.worker_closed and signal.getsignal(signal.SIGTERM) == previous
    assert_launcher_created_no_lock(rig)


@pytest.mark.parametrize("shutdown", ["sigterm", "input_failure"])
def test_closed_console_cannot_prevent_active_shutdown_stop(offline_launcher, monkeypatch, shutdown):
    from lynx import picker
    rig = offline_launcher
    calls, writes = 0, []

    def closed_print(*parts, **kwargs):
        writes.append(kwargs.get("file"))
        raise BrokenPipeError("offline console disconnected")

    def input_line(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "offline-action"
        assert rig.started.wait(5)
        if shutdown == "sigterm":
            signal.raise_signal(signal.SIGTERM)
        raise OSError("offline input disconnected")

    # Affect the launcher's output function, including the callback passed to
    # the real terminal stop worker, without replacing pytest's own output.
    monkeypatch.setattr(picker, "print", closed_print, raising=False)
    monkeypatch.setattr("builtins.input", input_line)
    previous = signal.getsignal(signal.SIGTERM)
    assert rig.run() == (0 if shutdown == "sigterm" else 1)
    assert rig.stop_calls == 1 and rig.finished.is_set()
    assert rig.session.motion_uncertain and rig.session.recovery.required()
    assert not rig.terminal.busy and rig.worker_closed
    assert signal.getsignal(signal.SIGTERM) == previous
    assert writes
    if shutdown == "input_failure":
        assert sys.stderr in writes  # The exception-reporting path also failed safely.
    assert_launcher_created_no_lock(rig)


@pytest.mark.parametrize("trim_message", ["released pages", "heap trim unavailable"])
@pytest.mark.parametrize("headroom_available", [True, False])
def test_linux_advises_cache_then_trims_then_requires_128_mib_before_worker(
        offline_launcher, monkeypatch, trim_message, headroom_available):
    from lynx import picker
    from lynx.resources import MemoryGuardError
    rig, events = offline_launcher, []
    fake_worker = picker.NativePerception

    def cache_advice():
        events.append("cache_advice")
        return SimpleNamespace(summary=lambda: "cache advice does not establish free memory")

    def capture_advice(folder, **options):
        assert folder == (rig.state / "captures").resolve() and options == {"sync": True}
        events.append("capture_cache")
        return SimpleNamespace(summary=lambda: "capture cache advice does not establish free memory")

    def native_advice():
        events.append("native_cache")
        return SimpleNamespace(summary=lambda: "native cache advice does not establish free memory")

    def trim():
        events.append("trim")
        return trim_message

    def guard(required):
        events.append(("guard", required))
        if not headroom_available:
            raise MemoryGuardError("offline measured headroom is below 128 MiB")
        return SimpleNamespace(summary=lambda amount: f"offline measured headroom passed {amount}")

    def worker(*args, **kwargs):
        events.append("worker")
        return fake_worker(*args, **kwargs)

    monkeypatch.setattr(picker, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    monkeypatch.setattr(picker, "advise_capture_cache", capture_advice)
    monkeypatch.setattr(picker, "advise_packaged_native_cache", native_advice)
    monkeypatch.setattr(picker, "advise_loaded_python_cache", cache_advice)
    monkeypatch.setattr(picker, "trim_unused_heap", trim)
    monkeypatch.setattr(picker, "wait_for_model_headroom", guard)
    monkeypatch.setattr(picker, "NativePerception", worker)
    monkeypatch.setattr("builtins.input", lambda _prompt: "quit")
    assert rig.run() == (0 if headroom_available else 1)
    assert events == ["capture_cache", "native_cache", "cache_advice", "trim", ("guard", 128)] + (["worker"] if headroom_available else [])
    assert rig.worker_closed is headroom_available
    assert rig.stop_calls == 0
    if not headroom_available:
        assert rig.session is None and rig.terminal is None
    assert_launcher_created_no_lock(rig)


def test_cli_scan_once_advises_only_completed_images_without_sync(offline_launcher, monkeypatch):
    from lynx import picker
    rig, advised = offline_launcher, []
    folder = (rig.state / "captures").resolve()
    paths = tuple(str(folder / name) for name in ("rgb_scan.png", "depth_scan.png"))
    inventory = {"images": dict(zip(("rgb", "depth"), paths))}
    def capture(purpose):
        assert purpose == "scan"
        for path in paths:
            Path(path).write_bytes(b"offline completed image")
        return inventory, 100.0
    def advice(root, *, paths):
        assert root == folder and all(Path(path).is_file() for path in paths)
        advised.append(paths)
    monkeypatch.setattr(picker, "sys", SimpleNamespace(platform="darwin", stderr=sys.stderr))
    monkeypatch.setattr(picker, "CaptureService", lambda *_args, **_kwargs: SimpleNamespace(capture=capture))
    monkeypatch.setattr(picker, "advise_capture_cache", advice)
    monkeypatch.setattr(picker, "print_cups", lambda value: None if value is inventory else pytest.fail("wrong inventory"))
    assert picker.main(["--state-dir", str(rig.state), "--model", "/offline/model", "--scan-once"]) == 0
    assert advised == [paths] and rig.worker_closed
    assert rig.stop_calls == 0


def test_linux_startup_wait_timeout_never_constructs_worker(offline_launcher, monkeypatch):
    from lynx import picker, resources
    rig = offline_launcher
    now, measurements = [0.0], []

    def insufficient(required, **_kwargs):
        measurements.append((now[0], required))
        raise resources.InsufficientHeadroomError("offline measured shortfall: 116.5 MiB free; 128 required")

    def sleep(duration):
        assert 0 < duration <= 0.2
        now[0] += duration

    def forbidden_worker(*_args, **_kwargs):
        pytest.fail("A timed-out startup must not construct the native worker")

    monkeypatch.setattr(picker, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    monkeypatch.setattr(resources, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(resources, "require_model_headroom", insufficient)
    monkeypatch.setattr(picker, "wait_for_model_headroom", resources.wait_for_model_headroom)
    monkeypatch.setattr(picker, "NativePerception", forbidden_worker)
    assert rig.run() == 1
    assert now[0] == 5.0 and len(measurements) > 1
    assert all(required == 128 and at < 5 for at, required in measurements)
    assert rig.session is None and rig.terminal is None and not rig.worker_closed
    assert rig.stop_calls == 0
    assert_launcher_created_no_lock(rig)
