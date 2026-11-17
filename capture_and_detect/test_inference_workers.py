"""Hardware-free protocol, kill/restart, HTTP and authentication regression tests."""

import base64
import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import numpy as np

from inference_backends import ProcessBackend, RemoteBackend, _clear_control_environment
from inference_protocol import (InferenceError, InferenceTimeout, MAX_RESPONSE_BYTES,
    ProtocolError, decode_detections, decode_request, json_bytes, load_json,
    make_request, make_response, make_settings, model_sha256, validate_response)


def _request(model_hash="a" * 64, request_id=None):
    return make_request(np.arange(45, dtype=np.uint8).reshape(3, 5, 3),
        model_sha256=model_hash, settings=make_settings(), observation_id="session:observation-1",
        request_id=request_id)


def _response(request):
    mask = np.zeros((request["height"], request["width"]), dtype=bool)
    mask[1:3, 1:4] = True
    return make_response(request, [{"class_name": "CUP BASE", "confidence": .95,
                         "bbox_xyxy": [1, 1, 4, 3], "mask": mask}], inference_ms=123)


def _fake_local_child(connection, model_path, expected_hash, settings, threads):
    """Spawn-safe fake; never imports torch/YOLO or opens camera/robot endpoints."""
    connection.send_bytes(b'{"ready":true}')
    try:
        while True:
            request = load_json(connection.recv_bytes(), 1024 * 1024)
            if request["request_id"] == "hang":
                time.sleep(60)
            if request["request_id"] == "crash":
                os._exit(1)
            response = _response(request)
            if request["request_id"] == "wrong":
                response["observation_id"] = "other-observation"
            connection.send_bytes(json_bytes(response, 1024 * 1024))
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


class ProtocolTests(unittest.TestCase):
    def test_child_environment_drops_inherited_control_credentials(self):
        with patch.dict(os.environ, {"GATEWAY_TOKEN": "fake", "GATEWAY_RWS_PASSWORD": "fake",
                                     "INFERENCE_TOKEN": "fake", "OMP_NUM_THREADS": "3"}, clear=True):
            _clear_control_environment()
            self.assertEqual(dict(os.environ), {"OMP_NUM_THREADS": "3"})

    def test_exact_lossless_noncontiguous_image_and_mask_round_trip(self):
        image = np.arange(45, dtype=np.uint8).reshape(3, 5, 3)[:, ::-1, :]
        request = make_request(image, model_sha256="a" * 64, settings=make_settings(), observation_id="obs")
        np.testing.assert_array_equal(decode_request(request), image)
        response = _response(request)
        self.assertIs(validate_response(response, request), response)
        decoded = decode_detections(response)[0]
        self.assertEqual(decoded["mask"].dtype, np.bool_)
        self.assertEqual(decoded["mask"].shape, (3, 5))
        self.assertEqual(int(decoded["mask"].sum()), 6)
        self.assertEqual(decoded["class_name"], "CUP BASE")

    def test_empty_detections_are_valid(self):
        request = _request()
        response = make_response(request, [])
        self.assertEqual(decode_detections(validate_response(response, request)), [])

    def test_every_observation_and_configuration_echo_is_bound(self):
        request = _request()
        changes = {"request_id": "late-request", "observation_id": "old-observation",
                   "width": 6, "height": 4, "image_sha256": "b" * 64,
                   "model_sha256": "b" * 64, "settings": make_settings(.6), "protocol_version": 2}
        for key, value in changes.items():
            with self.subTest(field=key):
                response = copy.deepcopy(_response(request))
                response[key] = value
                with self.assertRaises(ProtocolError):
                    validate_response(response, request)

    def test_corrupt_image_is_rejected(self):
        request = _request()
        request["image"]["data"] = base64.b64encode(b"x" * 45).decode()
        with self.assertRaises(ProtocolError):
            decode_request(request)

    def test_dimensions_are_bounded_before_decode(self):
        request = _request()
        request["height"] = request["width"] = 1000000
        with self.assertRaises(ProtocolError):
            decode_request(request)

    def test_invalid_masks_and_nonzero_padding_are_rejected(self):
        request = _request()
        for value in ("", "not base64", base64.b64encode(b"\xff\xff").decode(),
                      base64.b64encode(b"\x00\x00").decode()):
            with self.subTest(value=value):
                response = copy.deepcopy(_response(request))
                response["detections"][0]["mask"]["data"] = value
                with self.assertRaises(ProtocolError):
                    validate_response(response, request)

    def test_nonfinite_or_out_of_bounds_values_are_rejected(self):
        request = _request()
        for key, value in (("confidence", float("nan")), ("confidence", .1),
                           ("confidence", True), ("bbox_xyxy", [0, 0, float("inf"), 3]),
                           ("bbox_xyxy", [-1, 0, 2, 3]), ("bbox_xyxy", [3, 1, 1, 2]),
                           ("class_name", "ROBOT")):
            with self.subTest(field=key, value=value):
                response = copy.deepcopy(_response(request))
                response["detections"][0][key] = value
                with self.assertRaises(ProtocolError):
                    validate_response(response, request)

    def test_mask_pixels_must_belong_to_the_detection_box(self):
        request = _request()
        response = _response(request)
        response["detections"][0]["bbox_xyxy"] = [0, 0, 1, 1]
        with self.assertRaises(ProtocolError):
            validate_response(response, request)

    def test_settings_cannot_change_device_or_preprocessing(self):
        request = _request()
        for field, value in (("device", "mps"), ("rect", True), ("imgsz", 639), ("half", 0)):
            with self.subTest(field=field):
                bad = copy.deepcopy(request)
                bad["settings"][field] = value
                with self.assertRaises(ProtocolError):
                    decode_request(bad)

    def test_parser_rejects_duplicate_keys_nonfinite_and_oversize(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'null'):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                load_json(raw, 100)
        with self.assertRaises(ProtocolError):
            load_json(b'{"a":123}', 3)

    def test_existing_local_weight_file_is_required_and_hashed(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "weights.pt"
            with self.assertRaises(InferenceError):
                model_sha256(file)
            file.write_bytes(b"test weights")
            self.assertEqual(model_sha256(file), hashlib.sha256(b"test weights").hexdigest())


class ProcessBackendTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.weights = Path(self.folder.name) / "weights.pt"
        self.weights.write_bytes(b"fake test weights; never loaded by YOLO")
        self.backend = ProcessBackend(self.weights, make_settings(), startup_timeout_s=5)
        self.backend._target = _fake_local_child
        self.backend.start()

    def tearDown(self):
        self.backend.close()
        self.folder.cleanup()

    def request(self, request_id=None):
        return _request(self.backend.model_hash, request_id)

    def test_model_process_is_preloaded_and_reused(self):
        process = self.backend._process
        for _ in range(2):
            self.assertEqual(len(self.backend.predict(self.request(), 2)["detections"]), 1)
        self.assertIs(self.backend._process, process)
        self.assertTrue(self.backend.ready)

    def test_deadline_kills_stalled_process_and_next_request_restarts(self):
        started = time.monotonic()
        with self.assertRaises(InferenceTimeout):
            self.backend.predict(self.request("hang"), .1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.backend.ready)
        self.assertEqual(len(self.backend.predict(self.request(), 5)["detections"]), 1)

    def test_crashed_process_fails_closed_then_restarts(self):
        with self.assertRaises(InferenceError):
            self.backend.predict(self.request("crash"), 2)
        self.assertFalse(self.backend.ready)
        self.assertEqual(len(self.backend.predict(self.request(), 5)["detections"]), 1)

    def test_response_from_wrong_observation_never_passes(self):
        with self.assertRaises(ProtocolError):
            self.backend.predict(self.request("wrong"), 2)
        self.assertFalse(self.backend.ready)

    def test_close_interrupts_request_and_no_unbounded_queue(self):
        errors = []
        entered = threading.Event()
        def run():
            entered.set()
            try:
                self.backend.predict(self.request("hang"), 10)
            except InferenceError as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        entered.wait(1)
        until = time.monotonic() + 1
        while not self.backend._request_lock.locked() and time.monotonic() < until:
            time.sleep(.001)
        with self.assertRaisesRegex(InferenceError, "busy"):
            self.backend.predict(self.request(), 1)
        self.backend.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        with self.assertRaises(InferenceError):
            self.backend.predict(self.request(), 1)


class _HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.paths.append(self.path)
        if self.headers.get("Authorization") != "Bearer test-only-token":
            self.send_response(401)
            self.end_headers()
            return
        request = load_json(self.rfile.read(int(self.headers["Content-Length"])), 1024 * 1024)
        mode = self.server.mode
        if mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "/stolen-token")
            self.end_headers()
            return
        response = _response(request)
        if mode == "wrong":
            response["request_id"] = "different-request"
        body = json_bytes(response, 1024 * 1024) if mode != "invalid" else b'{"invalid":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(MAX_RESPONSE_BYTES + 1 if mode == "oversize" else len(body)))
        self.end_headers()
        try:
            if mode == "slow":
                for char in body:
                    self.wfile.write(bytes([char]))
                    self.wfile.flush()
                    time.sleep(.03)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class RemoteBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.mode = "valid"
        self.server.paths = []
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.backend = RemoteBackend(self.url, "test-only-token")

    def tearDown(self):
        self.backend.close()

    def test_authenticated_loopback_round_trip(self):
        request = _request()
        response = self.backend.predict(request, 5)
        self.assertEqual(response["request_id"], request["request_id"])
        self.assertEqual(self.server.paths, ["/infer"])

    def test_auth_failure_does_not_leak_address_or_token(self):
        bad = RemoteBackend(self.url, "secret-invalid-token")
        try:
            with self.assertRaises(InferenceError) as caught:
                bad.predict(_request(), 5)
            self.assertNotIn("secret-invalid-token", str(caught.exception))
            self.assertNotIn(self.url, str(caught.exception))
        finally:
            bad.close()

    def test_redirects_are_never_followed(self):
        self.server.mode = "redirect"
        with self.assertRaises(InferenceError):
            self.backend.predict(_request(), 5)
        self.assertEqual(self.server.paths, ["/infer"])

    def test_malformed_mismatched_and_oversize_responses_fail_closed(self):
        for mode in ("invalid", "wrong", "oversize"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                with self.assertRaises(InferenceError):
                    self.backend.predict(_request(), 5)

    def test_slow_drip_body_cannot_extend_total_deadline(self):
        self.server.mode = "slow"
        started = time.monotonic()
        with self.assertRaises(InferenceTimeout):
            self.backend.predict(_request(), .8)
        self.assertLess(time.monotonic() - started, 1.6)
        self.assertIsNone(self.backend._process)
        self.server.mode = "valid"
        self.assertEqual(len(self.backend.predict(_request(), 5)["detections"]), 1)

    def test_unsafe_addresses_and_header_tokens_are_rejected(self):
        for url in ("ftp://localhost", "http://name:password@localhost", "http://localhost#fragment"):
            with self.subTest(url=url), self.assertRaises(InferenceError):
                RemoteBackend(url, "token")
        with self.assertRaises(InferenceError):
            RemoteBackend(self.url, "token\r\nInjected: true")


class WorkerAPITests(unittest.TestCase):
    def test_worker_timeout_matches_backend_bounds(self):
        from inference_worker import create_app
        for timeout in (0, -1, float("nan"), float("inf"), 300.01, True):
            with self.subTest(timeout=timeout), self.assertRaises(InferenceError):
                create_app(object(), "test-only-token", request_timeout_s=timeout)
        self.assertIsNotNone(create_app(object(), "test-only-token", request_timeout_s=300))

    def test_service_requires_auth_bounds_body_and_has_no_control_routes(self):
        from fastapi.testclient import TestClient
        from inference_worker import create_app
        class Backend:
            ready = False
            def start(self):
                self.ready = True
            def close(self):
                self.ready = False
            def predict(self, request, timeout_s):
                decode_request(request)
                return _response(request)
        backend = Backend()
        app = create_app(backend, "test-only-token")
        headers = {"Authorization": "Bearer test-only-token"}
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 401)
            self.assertEqual(client.post("/infer", json=_request()).status_code, 401)
            self.assertEqual(client.get("/health", headers=headers).json(), {"ready": True, "protocol_version": 1})
            self.assertEqual(client.post("/infer", headers=headers, json=_request()).status_code, 200)
            self.assertEqual(client.post("/infer", headers=headers, json={}).status_code, 422)
            self.assertEqual(client.post("/infer", headers={**headers, "Content-Type": "application/json", "Content-Length": "999999999"}, content=b"{}").status_code, 413)
            self.assertEqual(client.post("/infer", headers={**headers, "Content-Type": "application/json", "Content-Encoding": "gzip"}, content=b"{}").status_code, 415)
            self.assertEqual(client.post("/robot/command", headers=headers, json={}).status_code, 404)
        self.assertFalse(backend.ready)


if __name__ == "__main__":
    unittest.main()
