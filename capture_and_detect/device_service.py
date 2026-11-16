"""Small authenticated HTTP adapter for a full picker, with no browser stack.

Only the chosen service loads a model. HTTP requests never directly dispatch
robot moves: they use the existing WebController's preview/confirmation worker.
"""
import argparse
import copy
from contextlib import ExitStack
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import threading
from types import SimpleNamespace

from device_agent import DeviceAgent, ExecutionLock, HandoffStore, read_json
from web_controller import WebError

MAX_DOCUMENT = 2 * 1024 * 1024
MAX_IMAGE = 24 * 1024 * 1024


def read_secret(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("The device access token must be a regular file.")
    value = path.read_text().strip()
    if not 32 <= len(value) <= 512 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("Invalid device access token.")
    return value


class PickerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = True

    def __init__(self, address, agent, token):
        self.agent, self.token = agent, token
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, PickerHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class PickerHandler(BaseHTTPRequestHandler):
    server_version = "CupPicker/1"
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Do not put headers, credentials or operation bodies in logs.

    def json_response(self, status, value):
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        if len(self.headers.get_all("Authorization", [])) != 1:
            return False
        value = self.headers.get("Authorization", "")
        return hmac.compare_digest(value.encode(), ("Bearer " + self.server.token).encode())

    def do_GET(self):
        if not self.authorized():
            self.json_response(401, {"detail": "Device authentication required."})
            return
        try:
            agent = self.server.agent
            routes = {"/v1/health": agent.health, "/v1/state": agent.state,
                      "/v1/handoff": agent.last_handoff}
            if self.path in routes:
                self.json_response(200, routes[self.path]())
                return
            match = re.fullmatch(r"/v1/image/([A-Za-z0-9_-]{1,150})", self.path)
            path = agent.image(match[1]) if match else None
            if path is None or path.stat().st_size > MAX_IMAGE:
                self.json_response(404, {"detail": "Image unavailable."})
                return
            # Stream from SD/disk instead of retaining another full image on Lynx.
            try:
                with path.open("rb") as stream:
                    size = os.fstat(stream.fileno()).st_size
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "image/png")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while chunk := stream.read(65536):
                        self.wfile.write(chunk)
            finally:
                if agent.native:
                    # Reading an old image charges its pages to the shared app
                    # budget again. Keep the saved file, but release that cache.
                    from lynx.resources import advise_capture_cache
                    root = getattr(agent.store, "capture_root", agent.store.root / "captures")
                    advise_capture_cache(root, paths=(path,))
        except WebError as error:
            self.json_response(error.status, {"detail": str(error)})
        except (OSError, ValueError, KeyError, TypeError):
            self.json_response(503, {"detail": "Device state unavailable; check its service log."})

    def do_POST(self):
        if not self.authorized():
            self.json_response(401, {"detail": "Device authentication required."})
            return
        try:
            if (self.headers.get("Transfer-Encoding") is not None
                    or len(self.headers.get_all("Content-Length", [])) != 1
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                raise WebError("Send a length-bounded JSON document.", 422)
            length = int(self.headers["Content-Length"])
            if not 1 <= length <= MAX_DOCUMENT:
                raise WebError("Device request is too large.", 413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise WebError("Incomplete request.", 422)
            from placing_grid import _unique_object, _invalid_constant
            body = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(body, dict):
                raise WebError("Expected a JSON object.", 422)
            routes = {"/v1/activate": self.server.agent.activate,
                      "/v1/deactivate": self.server.agent.deactivate,
                      "/v1/command": self.server.agent.submit}
            if self.path not in routes:
                raise WebError("Unknown device operation.", 404)
            self.json_response(200, routes[self.path](body))
        except WebError as error:
            self.json_response(error.status, {"detail": str(error)})
        except (ValueError, KeyError, TypeError, UnicodeError):
            self.json_response(422, {"detail": "Invalid device request or state."})
        except OSError:
            self.json_response(503, {"detail": "Device operation failed; refresh status before proceeding."})


def read_gateway_token(path):
    """Read the gateway credential without importing any device-specific runtime."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Gateway token must be a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened, current = os.fstat(stream.fileno()), os.lstat(path)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                or not os.path.samestat(before, opened) or not os.path.samestat(opened, current)):
            raise ValueError("Gateway token must be a regular file.")
        raw = stream.read(4097)
    token = raw.decode("ascii").strip()
    if len(raw) > 4096 or not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError("Gateway token file is invalid.")
    return token


def python_factory(args, *, runtime_paths=None, prefer_environment_token=False):
    """Full Python perception on Mac/Pi; dependencies load only on activation."""
    args = copy.deepcopy(args)
    paths = copy.deepcopy(runtime_paths or {})
    def configure_motion(settings):
        args.speed, args.camera_z = settings["speed"], settings["camera_z"]

    def factory(controller):
        from web_app import hardware_factory
        # Existing hardware factory reads this private, process-local credential.
        if not prefer_environment_token or not os.environ.get("GATEWAY_TOKEN"):
            os.environ["GATEWAY_TOKEN"] = read_gateway_token(args.state_dir / "gateway-token")
        options = SimpleNamespace(**vars(args), output=str(paths.get("output", args.state_dir / "captures")),
            calibration=str(paths.get("calibration", args.state_dir / "calibration.json")),
            placing_grid=str(paths.get("placing_grid", args.state_dir / "placing-grid.json")), grid_table_z=0.0,
            grid_cell_size=90.0, match_mm=20.0, match_px=40.0, llm=False,
            llm_model="qwen3-vl:4b-instruct", ollama_url="http://127.0.0.1:11434",
            mac_inference_url=None, isolated_inference=True, inference_model=str(args.model),
            inference_image_size=640, inference_threads=args.threads,
            inference_startup_timeout=90.0, inference_events=None,
            mac_timeout=5.0, pi_timeout=30.0, mac_probe_interval=3.0, mac_recovery_successes=3,
            verification_frames=1, strict_single_verification=True, scan_observations=1,
            execution_lock_held=True, discover_operator_controls=True)
        return hardware_factory(options)(controller)
    factory.metadata = lambda: {"backend": "python", "model": args.model.name,
                                "max_result_age_s": args.max_result_age, "depth_frames": args.depth_frames}
    factory.configure_motion = configure_motion
    return factory


def native_factory(args):
    """Create and dispose a new native worker for every selected-device period."""
    args = copy.deepcopy(args)
    current = None
    lock = threading.Lock()

    def configure_motion(settings):
        with lock:
            args.speed, args.camera_z = settings["speed"], settings["camera_z"]

    def factory(controller):
        nonlocal current
        from lynx.runtime import native_web_factory
        with lock:
            current = native_web_factory(args)
            active = current
        return active(controller)

    def metadata():
        with lock:
            active = current
        return active.metadata() if active else {
            "backend": "ncnn-cpp", "ready": False, "worker_running": False, "state": "not_started"}

    factory.metadata = metadata
    factory.configure_motion = configure_motion
    return factory


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", choices=("mac", "pi", "lynx"), required=True)
    parser.add_argument("--backend", choices=("python", "native"), required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--robot-id", required=True, help="Same deployment identity on all three devices")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--worker", default="/usr/local/bin/lynx-ncnn-worker")
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--depth-frames", type=int, choices=range(1, 10), default=5)
    parser.add_argument("--max-result-age", type=float, default=60.0)
    parser.add_argument("--inference-timeout", type=float, default=30.0)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--speed", type=float)
    parser.add_argument("--camera-z", action="store_true")
    parser.add_argument("--controller-profile", type=Path,
                        help="Optional read-only controller profile (default: STATE_DIR/controller-status.json)")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", args.robot_id):
        parser.error("robot-id must be 8–80 letters, digits, underscores or hyphens")
    from cup_inventory import finite_number
    if (not 1024 <= args.port <= 65535 or not 1 <= args.threads <= 32
            or not finite_number(args.conf) or not 0 < args.conf < 1
            or any(not finite_number(v) or not 0 < v <= 300 for v in (args.max_result_age, args.inference_timeout))):
        parser.error("Invalid service port, threads, confidence or timeout")
    if args.speed is not None:
        from robot_controls import valid_speed
        valid_speed(args.speed)
    args.state_dir = args.state_dir.resolve(strict=True)
    if args.controller_profile is None:
        args.controller_profile = args.state_dir / "controller-status.json"
    return args


def main(argv=None):
    args = arguments(argv)
    token = read_secret(args.token_file)
    with ExitStack() as stack:
        # Both locks are held even while inactive. A terminal on this device
        # cannot bypass the dashboard's handoff by opening the same state.
        stack.enter_context(ExecutionLock(args.state_dir / "picker.lock"))
        stack.enter_context(ExecutionLock(args.state_dir / ".placing-grid.json.controller.lock"))
        store = HandoffStore(args.state_dir, args.robot_id)
        store.identity()
        if args.backend == "native":
            factory = native_factory(args)
        else:
            factory = python_factory(args)
        agent = DeviceAgent(args.device_id, factory, store, live=args.live, native=args.backend == "native")
        server = PickerHTTPServer((args.listen, args.port), agent, token)
        stack.callback(server.server_close)
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        print(f"{args.device_id} picker service: {args.listen}:{args.port}; {'LIVE' if args.live else 'DRY RUN'}; waiting for dashboard activation", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            # Preserve authority; another host cannot take over just because
            # this process exited. Existing command completion is awaited.
            agent.close()
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
