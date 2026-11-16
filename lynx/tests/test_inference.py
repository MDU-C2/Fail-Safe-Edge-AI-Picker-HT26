import json
import errno
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

import lynx.inference as inference
import lynx.native_transport as transport
from lynx.inference import InferenceError, LynxDetector, NativeWorker
from capture_and_detect import Calibration, Detector


def model():
    return SimpleNamespace(param=Path("unused.param"), weights=Path("unused.bin"),
        input_size=320, names=("CUP", "CUP BASE", "CUP HANDLE", "TRAY"), precision="fp32", sha256="test")


@pytest.fixture
def child(tmp_path):
    path = tmp_path / "worker.py"
    path.write_text('''import json,struct,sys,time
mode=sys.argv[1]
def send(i,doc,masks=b"",size=None):
 b=json.dumps(doc).encode()
 sys.stdout.buffer.write(struct.pack("<4sIIII",b"LNXR",i,0,len(b),len(masks) if size is None else size)+b+masks)
 sys.stdout.buffer.flush()
ready={"ready":True,"protocol_version":1,"input_size":320,"classes_count":4,"max_detections":32}
if mode=="badready": ready["input_size"]=640
send(0,ready)
while True:
 header=sys.stdin.buffer.read(24)
 if not header: break
 magic,i,w,h,n,res=struct.unpack("<4sIIIII",header)
 payload=sys.stdin.buffer.read(n)
 if mode=="exit": sys.exit(0)
 if mode=="timeout": time.sleep(5)
 if mode=="stderr": print("worker diagnostic",file=sys.stderr,flush=True)
 item={"class_id":1,"score":.9,"box_xyxy":[1,1,4,4],"mask_bbox":[1,1,3,3],"mask_offset":0,"mask_length":9}
 doc={"width":w,"height":h,"inference_ms":2.3,"detections":[item]}
 if mode=="wrongshape": doc["width"]=w+1
 if mode=="badclass": item["class_id"]=99
 if mode=="nan": item["score"]=float("nan")
 if mode=="offset": item["mask_offset"]=1
 if mode=="bounds": item["mask_bbox"]=[w-1,1,3,3]
 if mode=="box": item["box_xyxy"]=[2,1,4,4]
 masks=bytes([2 if mode=="mask" else 1])*9
 send(i+1 if mode=="wrongid" else i,doc,masks,2**30 if mode=="oversize" else None)
''')
    return path


def make_worker(child, mode="ok"):
    return NativeWorker("unused", model(), command=[sys.executable, str(child), mode], timeout=.15)


def test_roundtrip_and_single_loaded_process(child):
    worker = make_worker(child)
    try:
        for _ in range(3):
            doc, masks = worker.infer(np.zeros((8, 8, 3), np.uint8))
            assert doc["detections"][0]["class_id"] == 1
            assert masks == bytes([1])*9
        assert worker.sequence == 3 and worker.process.poll() is None
    finally:
        worker.close()
    assert worker.process.poll() is not None


@pytest.mark.parametrize("mode", ["exit", "timeout", "wrongshape", "badclass", "nan", "offset", "bounds", "box", "mask", "wrongid", "oversize"])
def test_bad_result_kills_worker_and_cannot_be_reused(child, mode):
    worker = make_worker(child, mode)
    with pytest.raises(InferenceError):
        worker.infer(np.zeros((8, 8, 3), np.uint8))
    assert worker.process.poll() is not None
    with pytest.raises(InferenceError, match="unavailable"):
        worker.infer(np.zeros((8, 8, 3), np.uint8))


def test_readiness_shape_rejected(child):
    with pytest.raises(InferenceError, match="readiness"):
        make_worker(child, "badready")


def test_concurrent_request_rejected_without_killing_worker(child):
    worker = make_worker(child)
    try:
        with worker.lock:
            with pytest.raises(InferenceError, match="busy"):
                worker.infer(np.zeros((8, 8, 3), np.uint8))
        assert worker.process.poll() is None
    finally:
        worker.close()


@pytest.mark.parametrize("operation", ["read", "write", "stderr", "select"])
@pytest.mark.parametrize("exception", [BlockingIOError, InterruptedError])
def test_nonblocking_readiness_race_retries_without_corrupting_frames(child, monkeypatch, operation, exception):
    worker = make_worker(child, "stderr" if operation == "stderr" else "ok")
    injected = []
    errno_value = errno.EAGAIN if exception is BlockingIOError else errno.EINTR
    try:
        if operation == "select":
            original = inference.select.select
            def select_with_race(*args):
                if not injected:
                    injected.append(True)
                    raise exception(errno_value, "injected readiness race")
                return original(*args)
            monkeypatch.setattr(inference.select, "select", select_with_race)
        else:
            writing = operation == "write"
            descriptor = (worker.process.stdin if writing else worker.process.stderr
                          if operation == "stderr" else worker.process.stdout).fileno()
            original = inference.os.write if writing else inference.os.read
            progressed = []
            def syscall_with_race(fd, value):
                if fd == descriptor:
                    # Interrupt after a partial successful frame to prove that
                    # retries preserve offsets instead of repeating old bytes.
                    if not injected and (progressed or operation == "stderr"):
                        injected.append(True)
                        raise exception(errno_value, "injected readiness race")
                    progressed.append(True)
                    value = value[:7] if writing else min(value, 7)
                return original(fd, value)
            monkeypatch.setattr(inference.os, "write" if writing else "read", syscall_with_race)
        for _ in range(3):
            doc, masks = worker.infer(np.zeros((8, 8, 3), np.uint8))
            assert doc["width"] == 8 and masks == bytes([1]) * 9
        assert injected == [True]
        assert worker.process.poll() is None
        if operation == "stderr":
            assert b"worker diagnostic" in worker.error_log
    finally:
        worker.close()


@pytest.mark.parametrize("operation", ["read", "write", "stderr", "select"])
def test_persistent_readiness_races_keep_original_deadline_and_kill_worker(child, monkeypatch, operation):
    worker = make_worker(child)
    tick = [100.0]
    attempts = []
    def monotonic():
        tick[0] += .01
        return tick[0]
    clock = SimpleNamespace(monotonic=monotonic)
    monkeypatch.setattr(inference, "time", clock)
    monkeypatch.setattr(transport, "time", clock)
    if operation == "select":
        def interrupted_select(*args):
            attempts.append(True)
            raise InterruptedError(errno.EINTR, "persistent interrupt")
        monkeypatch.setattr(inference.select, "select", interrupted_select)
    else:
        writing = operation == "write"
        descriptor = (worker.process.stdin if writing else worker.process.stderr
                      if operation == "stderr" else worker.process.stdout).fileno()
        original = inference.os.write if writing else inference.os.read
        def unavailable(fd, value):
            if fd == descriptor:
                attempts.append(True)
                raise BlockingIOError(errno.EAGAIN, "persistent race")
            return original(fd, value)
        monkeypatch.setattr(inference.os, "write" if writing else "read", unavailable)
        if operation == "stderr":
            # Only stderr keeps appearing ready: draining it cannot postpone
            # the same request deadline indefinitely.
            monkeypatch.setattr(inference.select, "select", lambda *args: ([worker.process.stderr], [], []))
    with pytest.raises(InferenceError, match="timed out"):
        worker.infer(np.zeros((8, 8, 3), np.uint8))
    assert 1 <= len(attempts) <= 16
    assert worker.process.poll() is not None


@pytest.mark.parametrize("name", ["CUP", "CUP BASE", "CUP HANDLE"])
@pytest.mark.parametrize("origin", [(0, 0), (13, 17), (53, 49)])
@pytest.mark.parametrize("hole", [False, True])
@pytest.mark.parametrize("inset", [False, True])
def test_cropped_masks_preserve_existing_depth_and_robot_geometry(tmp_path, name, origin, hole, inset):
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps({"scale": 1.1, "R": [[1,0,0],[0,1,0],[0,0,1]], "t": [10,20,-500]}))
    calibration = Calibration(calibration_path)
    K = {"fx": 250., "fy": 260., "cx": 40., "cy": 35., "width": 80, "height": 70}
    x, y = origin
    roi = np.ones((19, 21), np.uint8)
    roi[:2, :4] = 0
    if inset:
        roi[:3, :] = 0
        roi[:, :5] = 0
        roi[-2:, :] = 0
        roi[:, -2:] = 0
    mask = np.zeros((70, 80), bool)
    mask[y:y+19, x:x+21] = roi
    depth = np.arange(70*80, dtype=np.uint16).reshape(70, 80) % 13 + 600
    if hole:
        depth[y+8:y+12, x+8:x+12] = 0
    expected = Detector.describe(SimpleNamespace(calibration=calibration,K=K), name, .9, mask, depth)
    entry = {"class_id": model().names.index(name), "score": .9, "box_xyxy": [x,y,x+21,y+19],
             "mask_bbox": [x,y,21,19], "mask_offset": 0, "mask_length": 399}
    worker = SimpleNamespace(model=model(), infer=lambda image: ({"detections": [entry], "inference_ms": 1.}, roi.tobytes()))
    actual, _ = LynxDetector(worker,calibration,K).analyse(np.zeros((70,80,3),np.uint8),depth)
    for key, value in expected.items():
        assert actual[0][key] == value, key


@pytest.mark.parametrize("name", ["CUP", "CUP BASE", "CUP HANDLE"])
@pytest.mark.parametrize("origin", [(0, 0), (13, 17), (59, 51)])
@pytest.mark.parametrize("condition", ["stable", "missing_latest", "moving"])
def test_cropped_temporal_depth_matches_full_image_guards(tmp_path, name, origin, condition):
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(json.dumps({"scale": 1., "R": [[1,0,0],[0,1,0],[0,0,1]], "t": [0,0,-750]}))
    calibration = Calibration(calibration_path)
    K = {"fx": 250., "fy": 260., "cx": 40., "cy": 35., "width": 80, "height": 70}
    x, y = origin
    roi = np.ones((19, 21), np.uint8)
    roi[:2, :4] = 0
    mask = np.zeros((70, 80), bool)
    mask[y:y+19, x:x+21] = roi
    samples = [np.full(mask.shape, value, np.uint16) for value in (800, 802, 804, 806, 808)]
    if condition == "missing_latest":
        samples[-1][mask] = 0
    elif condition == "moving":
        samples[-1][mask] = 950
    original = [sample.copy() for sample in samples]
    expected = Detector.describe(SimpleNamespace(calibration=calibration, K=K, _depth_samples=samples),
                                 name, .9, mask, samples[-1])
    entry = {"class_id": model().names.index(name), "score": .9, "box_xyxy": [x,y,x+21,y+19],
             "mask_bbox": [x,y,21,19], "mask_offset": 0, "mask_length": 399}
    worker = SimpleNamespace(model=model(), infer=lambda image: ({"detections": [entry], "inference_ms": 1.}, roi.tobytes()))
    detector = LynxDetector(worker, calibration, K)
    detector._depth_samples = tuple(samples)
    actual, _ = detector.analyse(np.zeros((70,80,3), np.uint8), samples[-1])
    for key, value in expected.items():
        assert actual[0][key] == value, key
    assert actual[0]["z_mm"] == (804 if condition == "stable" else None)
    for before, after in zip(original, samples):
        np.testing.assert_array_equal(before, after)
