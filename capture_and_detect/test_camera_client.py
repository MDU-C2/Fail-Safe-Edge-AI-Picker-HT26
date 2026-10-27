"""Offline camera protocol tests; no gateway or robot connection is made."""

import unittest
from unittest.mock import patch

import numpy as np
import requests

try:
    from .camera_client import CameraClient, CameraError
except ImportError:
    from camera_client import CameraClient, CameraError


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, label, *, headers=None, body=b"", json_data=None, status=200, events=None):
        self.label = label
        self.headers = headers or {}
        self.body = body
        self.json_data = json_data
        self.status_code = status
        self.events = events if events is not None else []
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if isinstance(self.json_data, Exception):
            raise self.json_data
        return self.json_data

    def iter_content(self, chunk_size):
        if self.closed:
            raise AssertionError("Tried to read a closed response")
        self.events.append(f"body:{self.label}")
        if isinstance(self.body, Exception):
            raise self.body
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses, events=None):
        self.responses = list(responses)
        self.events = events if events is not None else []
        self.calls = []

    def get(self, url, **kwargs):
        self.events.append("get:" + url.rsplit("/", 1)[1])
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected request: " + url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def status(sequence=10, **kwargs):
    return Response("status", json_data={"connected": True, "sequence": sequence}, **kwargs)


def frame(label, sequence=11, *, body=None, events=None, **headers):
    # An intentionally remote clock shows that freshness never compares clocks.
    values = {
        "X-Frame-Sequence": str(sequence),
        "X-Frame-Timestamp": "2001-02-03T04:05:06+00:00",
        "X-Frame-Width": "2",
        "X-Frame-Height": "1",
        "X-Pixel-Format": "BGR888p" if label == "rgb" else "RAW16",
    }
    values.update(headers)
    if body is None:
        body = bytes([1, 2, 3, 4, 5, 6]) if label == "rgb" else bytes([0x34, 0x12, 0x78, 0x56])
    return Response(label, headers=values, body=body, events=events)


class CameraClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        module = CameraClient.__module__
        self.monotonic = patch(module + ".time.monotonic", self.clock.monotonic)
        self.sleep = patch(module + ".time.sleep", self.clock.sleep)
        self.monotonic.start()
        self.sleep.start()
        self.addCleanup(self.monotonic.stop)
        self.addCleanup(self.sleep.stop)

    def client(self, responses, events=None):
        session = Session(responses, events)
        return CameraClient("http://offline.invalid/api/v1/", {"Authorization": "Bearer test"}, session), session

    def test_reads_both_headers_before_payloads_and_decodes_little_endian(self):
        events = []
        responses = [status(), frame("rgb", events=events), frame("depth", events=events)]
        client, session = self.client(responses, events)
        rgb, depth, metadata = client.capture()
        self.assertEqual(events, ["get:status", "get:rgb", "get:depth", "body:rgb", "body:depth"])
        np.testing.assert_array_equal(rgb, [[[1, 3, 5], [2, 4, 6]]])
        np.testing.assert_array_equal(depth, [[0x1234, 0x5678]])
        self.assertEqual(depth.dtype, np.dtype("uint16"))
        self.assertEqual(metadata["sequence"], 11)
        self.assertEqual(metadata["timestamp"], "2001-02-03T04:05:06+00:00")
        self.assertTrue(metadata["observed_at"].endswith("+00:00"))
        self.assertGreaterEqual(metadata["received_monotonic"], metadata["observation_started_monotonic"])
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(call[1]["stream"] for call in session.calls[1:]))

    def test_retry_time_is_included_in_observation_age(self):
        responses = [status(), frame("rgb", 11), frame("depth", 12),
                     frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        _, _, metadata = client.capture()
        self.assertEqual(metadata["observation_started_monotonic"], 100.0)
        self.assertGreater(metadata["received_monotonic"], 100.0)

    def test_sequence_race_retries_without_downloading_mismatched_bodies(self):
        events = []
        responses = [status(), frame("rgb", 11, events=events), frame("depth", 12, events=events),
                     frame("rgb", 13, events=events), frame("depth", 13, events=events)]
        client, _ = self.client(responses, events)
        _, _, metadata = client.capture()
        self.assertEqual(metadata["sequence"], 13)
        self.assertEqual(events.count("body:rgb"), 1)
        self.assertEqual(events.count("body:depth"), 1)
        self.assertTrue(all(response.closed for response in responses))

    def test_frozen_sequence_expires_and_closes_responses(self):
        responses = [status()]
        for _ in range(3):
            responses.extend([frame("rgb", 10), frame("depth", 10)])
        client, session = self.client(responses)
        with self.assertRaisesRegex(CameraError, "possibly frozen"):
            client.capture(timeout_s=0.11)
        self.assertAlmostEqual(self.clock.now, 100.11)
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(0 < call[1]["timeout"] <= 0.11 + 1e-10 for call in session.calls))

    def test_older_sequence_after_gateway_restart_is_rejected(self):
        responses = [status(100), frame("rgb", 1), frame("depth", 1)]
        client, _ = self.client(responses)
        with self.assertRaisesRegex(CameraError, "newer frame"):
            client.capture(timeout_s=0.01)
        self.assertTrue(all(response.closed for response in responses))

    def test_malformed_payload_size_is_rejected(self):
        for label, body in [("rgb", b"short"), ("rgb", b"toolong"), ("depth", b"\x00")]:
            with self.subTest(label=label, body=body):
                responses = [status(), frame("rgb", body=body if label == "rgb" else None),
                             frame("depth", body=body if label == "depth" else None)]
                client, _ = self.client(responses)
                with self.assertRaisesRegex(CameraError, "payload"):
                    client.capture()
                self.assertTrue(all(response.closed for response in responses))

    def test_invalid_formats_and_dimensions_are_rejected(self):
        cases = [
            {"X-Pixel-Format": "RGB888i"},
            {"X-Frame-Width": "0"},
            {"X-Frame-Height": "NaN"},
            {"X-Frame-Width": "2.0"},
            {"X-Frame-Sequence": "inf"},
            {"X-Frame-Sequence": "-1"},
            {"X-Frame-Timestamp": ""},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                responses = [status(), frame("rgb", **headers), frame("depth")]
                client, _ = self.client(responses)
                with self.assertRaises(CameraError):
                    client.capture()
                self.assertTrue(all(response.closed for response in responses))

    def test_different_aligned_depth_dimensions_or_timestamp_are_rejected(self):
        for headers in [{"X-Frame-Width": "3"}, {"X-Frame-Timestamp": "different"}, {"X-Pixel-Format": "GRAY8"}]:
            with self.subTest(headers=headers):
                responses = [status(), frame("rgb"), frame("depth", **headers)]
                client, _ = self.client(responses)
                with self.assertRaises(CameraError):
                    client.capture()
                self.assertTrue(all(response.closed for response in responses))

    def test_auth_errors_fail_immediately_and_close(self):
        for code in (401, 403):
            with self.subTest(code=code):
                response = Response("status", status=code)
                client, session = self.client([response])
                with self.assertRaisesRegex(CameraError, f"HTTP {code}"):
                    client.capture()
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(response.closed)

    def test_timeout_opening_depth_closes_rgb_and_retries(self):
        responses = [status(), frame("rgb"), requests.Timeout(), frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        self.assertEqual(client.capture()[2]["sequence"], 12)
        self.assertTrue(all(response.closed for response in responses if isinstance(response, Response)))

    def test_stream_download_timeout_retries_and_closes(self):
        responses = [status(), frame("rgb", body=requests.Timeout()), frame("depth"),
                     frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        self.assertEqual(client.capture()[2]["sequence"], 12)
        self.assertTrue(all(response.closed for response in responses))

    def test_transient_status_failure_retries(self):
        response = Response("status", status=503)
        client, _ = self.client([response, status(), frame("rgb"), frame("depth")])
        self.assertEqual(client.capture()[2]["sequence"], 11)
        self.assertTrue(response.closed)

    def test_disconnected_or_malformed_status_fails_closed(self):
        for data in ({"connected": False, "sequence": 10}, {"connected": True, "sequence": None},
                     {"connected": True, "sequence": True}, ValueError("not json"), []):
            with self.subTest(data=data):
                response = Response("status", json_data=data)
                client, _ = self.client([response])
                with self.assertRaises(CameraError):
                    client.capture()
                self.assertTrue(response.closed)

    def test_intrinsics_validates_values_and_closes(self):
        data = {"fx": 500.0, "fy": 500.0, "cx": 1, "cy": 2}
        response = Response("intrinsics", json_data=data)
        client, _ = self.client([response])
        self.assertEqual(client.intrinsics(), data)
        self.assertTrue(response.closed)
        for invalid in [dict(data, fx=float("nan")), dict(data, fy=0), dict(data, cx=True), []]:
            response = Response("intrinsics", json_data=invalid)
            client, _ = self.client([response])
            with self.assertRaises(CameraError):
                client.intrinsics()
            self.assertTrue(response.closed)

    def test_invalid_deadline_does_not_contact_gateway(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            client, session = self.client([])
            with self.assertRaises(CameraError):
                client.capture(value)
            self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
