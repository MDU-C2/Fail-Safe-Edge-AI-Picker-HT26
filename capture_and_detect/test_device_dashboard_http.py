"""ASGI concurrency checks with a blocked fake device proxy; no network/robot."""
import asyncio
import importlib.util
import threading
import unittest

from web_app import create_app
from web_controller import WebError

WEB_DEPENDENCIES = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx"))


class BlockingProxy:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.stop_received = threading.Event()
        self.calls = []

    def state(self):
        return {"instance_id": "test-router", "csrf_token": "test-csrf", "ready": True,
                "busy": self.entered.is_set() and not self.finished.is_set()}

    def status(self):
        return self.state()

    def submit(self, operation, payload):
        self.calls.append((operation, threading.get_ident()))
        if operation == "scan":
            self.entered.set()
            self.release.wait(3)
            self.finished.set()
        elif operation == "stop":
            self.stop_received.set()
        elif operation == "reject":
            raise WebError("Stale device ownership", 409)
        return {"accepted": True}


@unittest.skipUnless(WEB_DEPENDENCIES, "Install requirements-web.txt for HTTP tests")
class DeviceDashboardHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        self.proxy = BlockingProxy()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.proxy)),
                                       base_url="http://127.0.0.1:8080")
        self.headers = {"Origin": "http://127.0.0.1:8080", "X-CSRF-Token": "test-csrf"}

    async def asyncTearDown(self):
        self.proxy.release.set()
        await self.client.aclose()

    async def test_slow_proxy_submit_leaves_stop_and_status_responsive(self):
        scan = asyncio.create_task(self.client.post("/api/scan", headers=self.headers,
                                                   json={"request_id": "scan-test-123"}))
        try:
            self.assertTrue(await asyncio.to_thread(self.proxy.entered.wait, 1))
            self.assertFalse(self.proxy.finished.is_set(), "A synchronous proxy blocked the ASGI event loop")
            state, status = await asyncio.wait_for(asyncio.gather(
                self.client.get("/api/state"), self.client.get("/api/status")), timeout=1)
            self.assertEqual(state.status_code, 200)
            self.assertEqual(status.status_code, 200)
            self.assertTrue(state.json()["busy"])
            stop = await asyncio.wait_for(self.client.post("/api/stop", headers=self.headers,
                json={"request_id": "stop-test-123"}), timeout=1)
            self.assertEqual(stop.status_code, 202)
            self.assertTrue(self.proxy.stop_received.is_set())
            self.assertFalse(self.proxy.finished.is_set(), "Stop must arrive before the pending normal response")
            self.assertEqual([op for op, _ in self.proxy.calls], ["scan", "stop"])
            self.assertTrue(all(thread != threading.get_ident() for _, thread in self.proxy.calls))
        finally:
            self.proxy.release.set()
            result = await asyncio.wait_for(scan, timeout=2)
        self.assertEqual(result.status_code, 202)

    async def test_proxy_errors_keep_http_status_and_never_retry(self):
        result = await self.client.post("/api/reject", headers=self.headers,
                                        json={"request_id": "reject-test-123"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json(), {"detail": "Stale device ownership"})
        self.assertEqual([op for op, _ in self.proxy.calls], ["reject"])

    async def test_csrf_rejection_never_reaches_proxy_worker(self):
        result = await self.client.post("/api/stop", headers={"Origin": "http://127.0.0.1:8080"},
                                        json={"request_id": "stop-test-123"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.proxy.calls, [])


if __name__ == "__main__":
    unittest.main()
