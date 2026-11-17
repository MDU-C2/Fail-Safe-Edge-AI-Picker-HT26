"""Atomic RGB-D delivery and efficient waits, without camera hardware."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import weakref

import pytest
from fastapi import HTTPException

from gateway.camera import router
from gateway.camera.service import CameraService
from gateway.tests.support import Gateway, expect


async def publish(
    service: CameraService,
    value: int = 1,
    **kwargs,
):
    return await service.publish_frame_set(
        rgb_data=bytes([value]) * 6,
        depth_data=bytes([value + 10]) * 4,
        rgb_width=2,
        rgb_height=1,
        rgb_pixel_format="BGR888p",
        depth_width=2,
        depth_height=1,
        depth_pixel_format="RAW16",
        **kwargs,
    )


@pytest.fixture
def service(monkeypatch) -> CameraService:
    service = CameraService()
    monkeypatch.setattr(router, "camera_service", service)
    monkeypatch.setattr(
        router,
        "get_settings",
        lambda: SimpleNamespace(camera_max_frame_age_seconds=5.0),
    )
    return service


def test_pair_stream_holds_exact_pair_without_copying(service) -> None:
    async def scenario() -> None:
        first = await publish(service)
        response = await router.get_frame_pair(None)

        # A new frame arrives between creating and sending the response.
        await publish(service, value=2)
        chunks = [chunk async for chunk in response.body_iterator]

        assert chunks[0] is first.rgb.data
        assert chunks[1] is first.depth.data
        assert response.headers["X-Frame-Sequence"] == str(first.sequence)
        assert response.headers["X-Frame-Timestamp"] == first.timestamp.isoformat()
        assert response.headers["X-RGB-Length"] == "6"
        assert response.headers["X-Depth-Length"] == "4"
        assert response.headers["Content-Length"] == "10"

    asyncio.run(scenario())


def test_multiple_waiters_receive_new_pair_without_consuming_it(service) -> None:
    async def scenario() -> None:
        first = await publish(service)
        waiters = [
            asyncio.create_task(service.wait_for_frame_set(first.sequence, 1.0))
            for _ in range(3)
        ]
        await asyncio.sleep(0)
        assert all(not task.done() for task in waiters)

        second = await publish(service, value=2)
        results = await asyncio.wait_for(asyncio.gather(*waiters), 0.5)

        assert all(result is second for result in results)
        assert service.get_latest_frame_set() is second

    asyncio.run(scenario())


def test_wait_does_not_pin_obsolete_frame(service) -> None:
    async def scenario() -> None:
        first = await publish(service)
        old = weakref.ref(first)
        del first
        waiter = asyncio.create_task(service.wait_for_frame_set(1000, 1.0))
        await asyncio.sleep(0)

        await publish(service, value=2)
        await asyncio.sleep(0)
        assert old() is None

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    asyncio.run(scenario())


def test_wait_timeout_and_cancellation_leave_service_usable(service) -> None:
    async def scenario() -> None:
        first = await publish(service)
        assert await service.wait_for_frame_set(first.sequence, 0.01) is None

        waiter = asyncio.create_task(service.wait_for_frame_set(first.sequence, 30.0))
        await asyncio.sleep(0)
        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter

        second = await publish(service, value=2)
        assert await service.wait_for_frame_set(first.sequence, 0.0) is second

    asyncio.run(scenario())


def test_pair_wait_timeout_returns_no_body(service) -> None:
    async def scenario() -> None:
        first = await publish(service)
        response = await router.get_frame_pair(None, first.sequence, 0.01)

        assert response.status_code == 204
        assert response.body == b""

    asyncio.run(scenario())


def test_disconnect_wakes_waiter_and_refuses_cached_pair(service) -> None:
    async def scenario() -> None:
        first = await publish(service)
        waiter = asyncio.create_task(router.get_frame_pair(None, first.sequence, 30.0))
        await asyncio.sleep(0)
        await service.set_connected(False, error="camera unavailable")

        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(waiter, 0.5)

        assert error.value.status_code == 503
        assert "disconnected" in error.value.detail

    asyncio.run(scenario())


def test_stale_pair_is_refused_before_wait(service) -> None:
    async def scenario() -> None:
        first = await publish(
            service,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=10),
        )

        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(router.get_frame_pair(None, first.sequence, 30.0), 0.5)

        assert error.value.status_code == 503
        assert "stalled" in error.value.detail

    asyncio.run(scenario())


def test_pair_that_becomes_stale_during_wait_is_refused(service, monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            router,
            "get_settings",
            lambda: SimpleNamespace(camera_max_frame_age_seconds=0.01),
        )
        first = await publish(service)

        with pytest.raises(HTTPException) as error:
            await router.get_frame_pair(None, first.sequence, 0.03)

        assert error.value.status_code == 503
        assert "stalled" in error.value.detail

    asyncio.run(scenario())


def test_missing_or_incomplete_pair_is_refused(service) -> None:
    async def scenario() -> None:
        with pytest.raises(HTTPException) as error:
            await router.get_frame_pair(None)

        assert error.value.status_code == 503

        await service.publish_frame_set(
            rgb_data=b"\x00" * 6,
            depth_data=None,
            rgb_width=2,
            rgb_height=1,
            rgb_pixel_format="BGR888p",
            depth_width=2,
            depth_height=1,
            depth_pixel_format="RAW16",
        )

        with pytest.raises(HTTPException) as error:
            await router.get_frame_pair(None)

        assert error.value.status_code == 503
        assert "No depth frame" in error.value.detail

    asyncio.run(scenario())


def test_pair_wire_contract_and_next_frame(strict_gateway: Gateway) -> None:
    client = strict_gateway.client("diagnostics")
    response = client.get("/camera/frame")
    expect(response, 200)

    rgb_length = int(response.headers["X-RGB-Length"])
    depth_length = int(response.headers["X-Depth-Length"])
    assert rgb_length == 640 * 480 * 3
    assert depth_length == 640 * 480 * 2
    assert len(response.content) == rgb_length + depth_length
    assert int(response.headers["Content-Length"]) == len(response.content)
    assert response.headers["Content-Type"] == "application/x-rgbd-frame"
    assert response.headers["X-RGB-Pixel-Format"] == "BGR888p"
    assert response.headers["X-Depth-Pixel-Format"] == "RAW16"
    for prefix in ("RGB", "Depth"):
        assert response.headers[f"X-{prefix}-Width"] == "640"
        assert response.headers[f"X-{prefix}-Height"] == "480"

    # Known mock layout: the red plane is 128; the next byte begins depth.
    assert response.content[rgb_length - 1] == 128
    assert response.content[rgb_length:rgb_length + 4] == b"\x00\x00\xf4\x01"

    sequence = int(response.headers["X-Frame-Sequence"])
    newer = client.get(
        "/camera/frame",
        params={"after_sequence": sequence, "timeout_s": 3.0},
    )
    expect(newer, 200)
    assert int(newer.headers["X-Frame-Sequence"]) > sequence

    empty = client.get(
        "/camera/frame",
        params={"after_sequence": sequence + 1000, "timeout_s": 0.01},
    )
    expect(empty, 204)
    assert empty.content == b""


@pytest.mark.parametrize("params", [
    {"after_sequence": -1},
    {"timeout_s": -1},
    {"timeout_s": 31},
    {"timeout_s": "nan"},
])
def test_pair_wait_parameters_are_bounded(strict_gateway: Gateway, params) -> None:
    expect(strict_gateway.client("diagnostics").get("/camera/frame", params=params), 422)
