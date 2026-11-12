"""Shared-memory camera captures on an actual background loop, no hardware."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from gateway.camera.service import CameraService
from gateway.models.camera import CameraIntrinsics
from lynx.camera import CameraError, LocalCameraClient


class SignalledCameraService(CameraService):
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self.transform = lambda pair: pair
        self.requested_sequence = None

    async def wait_for_frame_set(self, after_sequence, timeout_seconds):
        self.requested_sequence = after_sequence
        self.waiting.set()
        pair = await super().wait_for_frame_set(after_sequence, timeout_seconds)
        return self.transform(pair) if pair is not None else None


def submit(loop, coroutine):
    return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=2.0)


async def publish(service, **kwargs):
    return await service.publish_frame_set(
        rgb_data=bytes([1, 2, 3, 4, 5, 6]),
        depth_data=bytes([0x34, 0x12, 0x78, 0x56]),
        rgb_width=2,
        rgb_height=1,
        rgb_pixel_format="BGR888p",
        depth_width=2,
        depth_height=1,
        depth_pixel_format="RAW16",
        **kwargs,
    )


def intrinsics(**updates):
    return CameraIntrinsics(
        **{
            "width": 2, "height": 1,
            "fx": 500.0, "fy": 500.0, "cx": 1.0, "cy": 0.5,
            "matrix": [[500.0, 0.0, 1.0], [0.0, 500.0, 0.5], [0.0, 0.0, 1.0]],
            **updates,
        },
    )


@pytest.fixture
def camera():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(timeout=2.0)
    service = SignalledCameraService()
    service.set_intrinsics(intrinsics())
    submit(loop, publish(service))
    client = LocalCameraClient(service, loop, api="http://127.0.0.1:8000/api/v1")

    try:
        yield client, service, loop
    finally:
        async def cancel_pending():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        submit(loop, cancel_pending())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        loop.close()


def capture_new(camera, *, during_wait=None, timestamp=None):
    client, service, loop = camera
    service.waiting.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.capture, 1.0)
        assert service.waiting.wait(timeout=1.0)
        assert not future.done()
        if during_wait is not None:
            during_wait()
        pair = submit(loop, publish(service, timestamp=timestamp))
        return future.result(timeout=2.0), pair


def test_capture_waits_for_new_pair_and_decodes_without_depth_copy(camera):
    client, service, loop = camera
    baseline = service.get_status().sequence
    (rgb, depth, metadata), pair = capture_new(camera)

    assert service.requested_sequence == baseline
    assert metadata["sequence"] == pair.sequence > baseline
    assert metadata["timestamp"] == pair.timestamp.isoformat()
    assert metadata["received_monotonic"] >= metadata["observation_started_monotonic"]
    assert datetime.fromisoformat(metadata["observed_at"]).tzinfo is not None
    np.testing.assert_array_equal(rgb, [[[1, 3, 5], [2, 4, 6]]])
    np.testing.assert_array_equal(depth, [[0x1234, 0x5678]])
    assert rgb.flags.c_contiguous and rgb.flags.owndata
    assert not depth.flags.writeable
    assert np.shares_memory(depth, np.frombuffer(pair.depth.data, dtype="<u2"))
    assert not np.shares_memory(rgb, np.frombuffer(pair.rgb.data, dtype=np.uint8))

    # A later publication cannot invalidate the immutable retained depth bytes.
    submit(loop, publish(service))
    np.testing.assert_array_equal(depth, [[0x1234, 0x5678]])
    with pytest.raises(ValueError):
        depth[0, 0] = 0


def test_frozen_camera_times_out_instead_of_reusing_baseline(camera):
    client, service, _ = camera
    started = time.monotonic()
    with pytest.raises(CameraError, match="fresh|Timed out"):
        client.capture(timeout_s=0.02)
    assert time.monotonic() - started < 0.5
    assert service.get_status().sequence == 1


def test_disconnected_camera_refuses_retained_frame(camera):
    client, service, loop = camera
    submit(loop, service.set_connected(False, error="camera link lost"))
    with pytest.raises(CameraError, match="not published"):
        client.capture()
    assert not service.waiting.is_set()


def test_disconnect_interrupts_long_capture(camera):
    client, service, loop = camera
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.capture, 30.0)
        assert service.waiting.wait(timeout=1.0)
        submit(loop, service.set_connected(False, error="camera link lost"))
        with pytest.raises(CameraError, match="disconnected"):
            future.result(timeout=1.0)


def test_cancelled_loop_wait_is_reported_as_camera_error(camera):
    _, service, _ = camera

    def cancel(_):
        raise asyncio.CancelledError()

    service.transform = cancel
    with pytest.raises(CameraError, match="cancelled"):
        capture_new(camera)


def test_outer_deadline_cancels_an_unresponsive_wait(camera, monkeypatch):
    client, service, _ = camera
    cancelled = threading.Event()

    async def unresponsive_wait(*_):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(service, "wait_for_frame_set", unresponsive_wait)
    with pytest.raises(CameraError, match="Timed out"):
        client.capture(timeout_s=0.01)
    assert cancelled.wait(timeout=1.0)


def test_reconnection_requires_a_new_frame(camera):
    client, service, loop = camera
    baseline = service.get_status().sequence
    submit(loop, service.set_connected(False))
    submit(loop, service.set_connected(True))
    with pytest.raises(CameraError, match="fresh|Timed out"):
        client.capture(timeout_s=0.01)
    (_, _, metadata), _ = capture_new(camera)
    assert metadata["sequence"] > baseline


def test_changed_intrinsics_during_wait_refuses_observation(camera):
    _, service, _ = camera
    with pytest.raises(CameraError, match="intrinsics changed"):
        capture_new(camera, during_wait=lambda: service.set_intrinsics(intrinsics(fx=510.0)))


def test_intrinsics_image_size_mismatch_is_refused(camera):
    _, service, _ = camera
    service.set_intrinsics(intrinsics(width=3))
    with pytest.raises(CameraError, match="dimensions.*intrinsics"):
        capture_new(camera)


@pytest.mark.parametrize("offset", [-10, 10])
def test_stale_or_future_new_sequence_is_refused(camera, offset):
    with pytest.raises(CameraError, match="stale|future"):
        capture_new(camera, timestamp=datetime.now(timezone.utc) + timedelta(seconds=offset))


def test_timezone_free_new_frame_is_refused(camera):
    with pytest.raises(CameraError, match="timezone"):
        capture_new(camera, timestamp=datetime.now())


@pytest.mark.parametrize("change", [
    lambda pair: replace(pair, rgb=None),
    lambda pair: replace(pair, depth=None),
    lambda pair: replace(pair, sequence=1),
    lambda pair: replace(pair, sequence=True),
    lambda pair: replace(pair, rgb=replace(pair.rgb, sequence=99)),
    lambda pair: replace(pair, depth=replace(pair.depth, sequence=99)),
    lambda pair: replace(pair, depth=replace(pair.depth, timestamp=pair.timestamp - timedelta(seconds=1))),
    lambda pair: replace(pair, depth=replace(pair.depth, width=3)),
    lambda pair: replace(pair, rgb=replace(pair.rgb, width=2.0), depth=replace(pair.depth, width=2.0)),
    lambda pair: replace(pair, rgb=replace(pair.rgb, width=0), depth=replace(pair.depth, width=0)),
    lambda pair: replace(pair, rgb=replace(pair.rgb, pixel_format="RGB888i")),
    lambda pair: replace(pair, depth=replace(pair.depth, pixel_format="GRAY8")),
    lambda pair: replace(pair, rgb=replace(pair.rgb, data=b"short")),
    lambda pair: replace(pair, depth=replace(pair.depth, data=b"short")),
    lambda pair: replace(pair, depth=replace(pair.depth, data=bytearray(pair.depth.data))),
])
def test_malformed_pair_fails_closed(camera, change):
    _, service, _ = camera
    service.transform = change
    with pytest.raises(CameraError):
        capture_new(camera)


@pytest.mark.parametrize("data", [
    {"width": True}, {"height": 0}, {"fx": float("nan")}, {"fy": 0},
    {"cx": True}, {"cy": None},
])
def test_invalid_intrinsics_are_rejected(camera, data):
    client, service, _ = camera
    malformed = {**intrinsics().model_dump(), **data}
    service.set_intrinsics(SimpleNamespace(model_dump=lambda: malformed))
    with pytest.raises(CameraError, match="Invalid camera"):
        client.intrinsics()


def test_missing_intrinsics_are_rejected(camera):
    client, service, _ = camera
    service.set_intrinsics(None)
    with pytest.raises(CameraError, match="not ready"):
        client.intrinsics()


def test_same_loop_capture_is_rejected_before_it_can_deadlock(camera):
    client, _, loop = camera

    async def on_gateway_loop():
        with pytest.raises(CameraError, match="coordinator thread"):
            client.capture()

    submit(loop, on_gateway_loop())


@pytest.mark.parametrize("closed", [False, True])
def test_stopped_or_closed_loop_fails_without_scheduling(closed):
    loop = asyncio.new_event_loop()
    if closed:
        loop.close()
    client = LocalCameraClient(CameraService(), loop, api="local")
    try:
        with pytest.raises(CameraError, match="not running"):
            client.capture()
    finally:
        loop.close()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "1"])
def test_invalid_timeout_is_rejected_without_waiting(camera, value):
    client, service, _ = camera
    with pytest.raises(CameraError, match="positive and finite"):
        client.capture(value)
    assert not service.waiting.is_set()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_max_age_is_rejected(value):
    with pytest.raises(ValueError, match="positive and finite"):
        LocalCameraClient(None, None, api="local", max_age=value)
