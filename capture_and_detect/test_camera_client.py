"""Offline camera protocol tests; no gateway or robot connection is made."""

import unittest
from unittest.mock import patch

import numpy as np
import requests

try:
    from .camera_client import CameraClient, CameraError, NoNewCameraFrame, TransientCameraError
except ImportError:
    from camera_client import CameraClient, CameraError, NoNewCameraFrame, TransientCameraError


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
        responses = [status(), status(11), frame("rgb", events=events), frame("depth", events=events)]
        client, session = self.client(responses, events)
        rgb, depth, metadata = client.capture()
        self.assertEqual(events, ["get:status", "get:status", "get:rgb", "get:depth", "body:rgb", "body:depth"])
        np.testing.assert_array_equal(rgb, [[[1, 3, 5], [2, 4, 6]]])
        np.testing.assert_array_equal(depth, [[0x1234, 0x5678]])
        self.assertEqual(depth.dtype, np.dtype("uint16"))
        self.assertEqual(metadata["sequence"], 11)
        self.assertEqual(metadata["timestamp"], "2001-02-03T04:05:06+00:00")
        self.assertTrue(metadata["observed_at"].endswith("+00:00"))
        self.assertGreaterEqual(metadata["received_monotonic"], metadata["observation_started_monotonic"])
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(call[1]["stream"] for call in session.calls[2:]))
        self.assertTrue(all(call[1]["headers"]["Connection"] == "close" for call in session.calls))

    def test_retry_time_is_included_in_observation_age(self):
        responses = [status(), status(11), frame("rgb", 11), frame("depth", 12),
                     status(12), frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        _, _, metadata = client.capture()
        self.assertEqual(metadata["observation_started_monotonic"], 100.0)
        self.assertGreater(metadata["received_monotonic"], 100.0)

    def test_waits_for_new_status_before_opening_image_streams(self):
        events = []
        responses = [status(), status(), status(), status(11),
                     frame("rgb", events=events), frame("depth", events=events)]
        client, session = self.client(responses, events)
        _, _, metadata = client.capture()
        self.assertEqual(metadata["sequence"], 11)
        self.assertEqual(events, ["get:status"] * 4 +
                         ["get:rgb", "get:depth", "body:rgb", "body:depth"])
        self.assertAlmostEqual(self.clock.now, 100.1)
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(call[1]["headers"]["Connection"] == "close" for call in session.calls))

    def test_pair_race_rechecks_status_without_replacing_original_baseline(self):
        events = []
        responses = [status(), status(11), frame("rgb", 11, events=events),
                     frame("depth", 12, events=events), status(10), status(13),
                     frame("rgb", 13, events=events), frame("depth", 13, events=events)]
        client, _ = self.client(responses, events)
        _, _, metadata = client.capture()
        self.assertEqual(metadata["sequence"], 13)
        self.assertEqual(events, ["get:status", "get:status", "get:rgb", "get:depth",
                                 "get:status", "get:status", "get:rgb", "get:depth",
                                 "body:rgb", "body:depth"])
        self.assertEqual(metadata["observation_started_monotonic"], 100.0)
        self.assertTrue(all(response.closed for response in responses))

    def test_status_poll_failure_retries_with_original_baseline(self):
        events = []
        responses = [status(), Response("status", status=503), status(11),
                     frame("rgb", events=events), frame("depth", events=events)]
        client, _ = self.client(responses, events)
        self.assertEqual(client.capture()[2]["sequence"], 11)
        self.assertEqual(events, ["get:status"] * 3 +
                         ["get:rgb", "get:depth", "body:rgb", "body:depth"])
        self.assertTrue(all(response.closed for response in responses))

    def test_ready_status_does_not_authorize_a_stale_image_pair(self):
        responses = [status(), status(11), frame("rgb", 10), frame("depth", 10)]
        client, _ = self.client(responses)
        with self.assertRaises(NoNewCameraFrame):
            client.capture(timeout_s=.01)
        self.assertTrue(all(response.closed for response in responses))

    def test_matching_pair_may_advance_beyond_ready_status(self):
        responses = [status(), status(11), frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        self.assertEqual(client.capture()[2]["sequence"], 12)
        self.assertTrue(all(response.closed for response in responses))

    def test_exhausted_readiness_failures_keep_original_deadline_and_http_cause(self):
        responses = [status()] + [Response("status", status=503) for _ in range(3)]
        client, session = self.client(responses)
        with self.assertRaisesRegex(TransientCameraError, "HTTP 503") as caught:
            client.capture(timeout_s=.11)
        self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in session.calls], ["status"] * 4)
        self.assertIsInstance(caught.exception.__cause__.__cause__, requests.HTTPError)
        self.assertAlmostEqual(self.clock.now, 100.11)
        self.assertTrue(all(response.closed for response in responses))

    def test_five_fresh_four_hz_samples_open_only_ten_image_streams(self):
        clock = self.clock
        class PublishingSession:
            def __init__(self):
                self.paths, self.responses = [], []

            def get(self, url, **kwargs):
                path = url.rsplit("/", 1)[-1]
                self.paths.append(path)
                sequence = 10 + int((clock.now - 100.0) * 4)
                response = status(sequence) if path == "status" else frame(path, sequence)
                self.responses.append(response)
                return response

        session = PublishingSession()
        client = CameraClient("http://offline.invalid", {}, session)
        frames = [client.capture(timeout_s=2)[2] for _ in range(5)]
        self.assertEqual([value["sequence"] for value in frames], list(range(11, 16)))
        self.assertEqual(session.paths.count("rgb"), 5)
        self.assertEqual(session.paths.count("depth"), 5)
        self.assertGreater(session.paths.count("status"), 10)
        self.assertTrue(all(response.closed for response in session.responses))

    def test_v4_latest_pair_has_no_newer_than_status_gate(self):
        responses = [frame("rgb", 10), frame("depth", 10)]
        client, session = self.client(responses)
        _, depth, metadata = client.capture_latest()
        self.assertEqual(metadata["sequence"], 10)
        self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in session.calls], ["rgb", "depth"])
        np.testing.assert_array_equal(depth, [[0x1234, 0x5678]])
        self.assertTrue(all(response.closed for response in responses))

    def test_latest_pair_still_rejects_a_mismatched_payload(self):
        client, _ = self.client([frame("rgb", 10, body=b"short"), frame("depth", 10)])
        with self.assertRaisesRegex(CameraError, "bytes"):
            client.capture_latest()

    def test_sequence_race_retries_without_downloading_mismatched_bodies(self):
        events = []
        responses = [status(), status(11), frame("rgb", 11, events=events), frame("depth", 12, events=events),
                     status(13), frame("rgb", 13, events=events), frame("depth", 13, events=events)]
        client, _ = self.client(responses, events)
        _, _, metadata = client.capture()
        self.assertEqual(metadata["sequence"], 13)
        self.assertEqual(events.count("body:rgb"), 1)
        self.assertEqual(events.count("body:depth"), 1)
        self.assertTrue(all(response.closed for response in responses))

    def test_frozen_sequence_expires_and_closes_responses(self):
        responses = [status() for _ in range(4)]
        client, session = self.client(responses)
        with self.assertRaisesRegex(NoNewCameraFrame, "possibly frozen") as caught:
            client.capture(timeout_s=0.11)
        self.assertIsInstance(caught.exception.__cause__, NoNewCameraFrame)
        self.assertAlmostEqual(self.clock.now, 100.11)
        self.assertEqual([url.rsplit("/", 1)[-1] for url, _ in session.calls], ["status"] * 4)
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(0 < call[1]["timeout"] <= 0.11 + 1e-10 for call in session.calls))

    def test_older_sequence_after_gateway_restart_is_rejected(self):
        responses = [status(100), status(1)]
        client, _ = self.client(responses)
        with self.assertRaisesRegex(CameraError, "newer frame"):
            client.capture(timeout_s=0.01)
        self.assertTrue(all(response.closed for response in responses))

    def test_malformed_payload_size_is_rejected(self):
        for label, body in [("rgb", b"short"), ("rgb", b"toolong"), ("depth", b"\x00")]:
            with self.subTest(label=label, body=body):
                responses = [status(), status(11), frame("rgb", body=body if label == "rgb" else None),
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
                responses = [status(), status(11), frame("rgb", **headers), frame("depth")]
                client, _ = self.client(responses)
                with self.assertRaises(CameraError):
                    client.capture()
                self.assertTrue(all(response.closed for response in responses))

    def test_different_aligned_depth_dimensions_or_timestamp_are_rejected(self):
        for headers in [{"X-Frame-Width": "3"}, {"X-Frame-Timestamp": "different"}, {"X-Pixel-Format": "GRAY8"}]:
            with self.subTest(headers=headers):
                responses = [status(), status(11), frame("rgb"), frame("depth", **headers)]
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
        responses = [status(), status(11), frame("rgb"), requests.Timeout(),
                     status(12), frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        self.assertEqual(client.capture()[2]["sequence"], 12)
        self.assertTrue(all(response.closed for response in responses if isinstance(response, Response)))

    def test_stream_download_timeout_retries_and_closes(self):
        responses = [status(), status(11), frame("rgb", body=requests.Timeout()), frame("depth"),
                     status(12), frame("rgb", 12), frame("depth", 12)]
        client, _ = self.client(responses)
        self.assertEqual(client.capture()[2]["sequence"], 12)
        self.assertTrue(all(response.closed for response in responses))

    def test_transient_status_failure_retries(self):
        response = Response("status", status=503)
        client, _ = self.client([response, status(), status(11), frame("rgb"), frame("depth")])
        self.assertEqual(client.capture()[2]["sequence"], 11)
        self.assertTrue(response.closed)

    def test_exhausted_transient_status_keeps_type_and_cause(self):
        for code in (408, 429, 500, 503, 504):
            with self.subTest(code=code):
                replies = [Response("status", status=code) for _ in range(3)]
                client, session = self.client(replies)
                with self.assertRaisesRegex(TransientCameraError, f"HTTP {code}") as caught:
                    client.capture(timeout_s=0.11)
                self.assertEqual(len(session.calls), 3)
                self.assertTrue(all(reply.closed for reply in replies))
                self.assertIsInstance(caught.exception.__cause__, TransientCameraError)
                self.assertIsInstance(caught.exception.__cause__.__cause__, requests.HTTPError)

    def test_exhausted_depth_service_failure_closes_open_rgb_response(self):
        replies = [status(), status(11), frame("rgb"), Response("depth", status=503)]
        client, session = self.client(replies)
        with self.assertRaisesRegex(TransientCameraError, "/camera/depth failed") as caught:
            client.capture(timeout_s=0.01)
        self.assertEqual(len(session.calls), 4)
        self.assertTrue(all(reply.closed for reply in replies))
        self.assertIsInstance(caught.exception.__cause__.__cause__, requests.HTTPError)

    def test_exhausted_transport_and_stream_failures_remain_retryable(self):
        for fault in (requests.ConnectionError("disconnected"), requests.Timeout("slow"),
                      requests.exceptions.ChunkedEncodingError("truncated stream")):
            with self.subTest(fault=type(fault).__name__):
                client, _ = self.client([fault])
                with self.assertRaises(TransientCameraError) as caught:
                    client.capture(timeout_s=0.01)
                self.assertIs(caught.exception.__cause__.__cause__, fault)
        fault = requests.exceptions.ChunkedEncodingError("stream ended")
        replies = [status(), status(11), frame("rgb", body=fault), frame("depth")]
        client, _ = self.client(replies)
        with self.assertRaises(TransientCameraError) as caught:
            client.capture(timeout_s=0.01)
        self.assertIs(caught.exception.__cause__.__cause__, fault)
        self.assertTrue(all(reply.closed for reply in replies))

    def test_auth_and_malformed_frames_never_become_transient(self):
        cases = [[Response("status", status=code)] for code in (400, 401, 403, 404)]
        cases += [[Response("status", json_data={"connected": True, "sequence": "invalid"})],
                  [status(), status(11), frame("rgb", body=b"short"), frame("depth")]]
        for replies in cases:
            with self.subTest(replies=replies):
                client, _ = self.client(replies)
                with self.assertRaises(CameraError) as caught:
                    client.capture(timeout_s=0.01)
                self.assertNotIsInstance(caught.exception, TransientCameraError)
                self.assertTrue(all(reply.closed for reply in replies))

    def test_gateway_connection_policy_overrides_keep_alive_without_mutating_headers(self):
        headers = {"Authorization": "Bearer test", "connection": "keep-alive"}
        client = CameraClient("http://offline.invalid", headers, Session([]))
        self.assertEqual(headers["connection"], "keep-alive")
        self.assertEqual(client.headers, {"Authorization": "Bearer test", "Connection": "close"})

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
