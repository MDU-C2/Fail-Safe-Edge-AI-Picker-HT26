"""Full dashboard presentation backed only by fixed, read-only gateway requests.

This adapter has no picker, device ownership, model, writable state or command
client. Camera images are gateway frames, never labelled as worker detections.
"""

import copy
from collections import OrderedDict
from datetime import datetime, timezone
import io
import secrets
import threading
import time
import uuid

import requests

from gateway_observer import DemoObserver, GatewayObserver, diagnostics_token, utc_now
from robot_telemetry import RobotTelemetry

ROLE_NAMES = {"primary": "Primary computer", "secondary": "Raspberry Pi", "tertiary": "Lynx"}
ROLE_IDS = {"primary": "cloud", "secondary": "pi", "tertiary": "lynx"}
MAX_IMAGE = 16 * 1024 * 1024


def camera_jpeg(raw, headers):
    """Bound raw RGB and encoded images before allocating or decoding pixels."""
    from PIL import Image
    content_type = headers.get("Content-Type", "").split(";")[0].lower()
    if content_type in ("image/jpeg", "image/png"):
        frame = Image.open(io.BytesIO(raw))
        if frame.format not in ("JPEG", "PNG") or not 0 < frame.width * frame.height <= 4096 * 2160:
            raise ValueError("Unsupported camera image.")
        frame = frame.convert("RGB")
    else:
        width, height = int(headers.get("X-Frame-Width", 0)), int(headers.get("X-Frame-Height", 0))
        pixel_format = headers.get("X-Pixel-Format", "").lower()
        if (pixel_format not in ("rgb8", "bgr8", "bgr888p") or not 0 < width <= 4096
                or not 0 < height <= 2160 or len(raw) != width * height * 3):
            raise ValueError("Unsupported camera image.")
        if pixel_format == "bgr888p":
            size = width * height
            channels = [Image.frombytes("L", (width, height), raw[offset:offset + size])
                        for offset in (2 * size, size, 0)]
            frame = Image.merge("RGB", channels)
        else:
            frame = Image.frombytes("RGB", (width, height), raw, "raw", "BGR" if pixel_format == "bgr8" else "RGB")
    frame.thumbnail((1280, 960))
    output = io.BytesIO()
    frame.save(output, format="JPEG", quality=82)
    return output.getvalue()


class CameraReader:
    """One demand-driven camera GET every two seconds; bounded local image cache."""
    def __init__(self, api, headers, *, clock=time.monotonic):
        self.api, self.headers, self.clock = api, dict(headers), clock
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.demand_until = 0
        self.latest = None
        self.failed = False
        self.images = OrderedDict()

    def snapshot(self, *, demand=False):
        with self.lock:
            if demand and not self.stop.is_set():
                self.demand_until = self.clock() + 3
                if self.thread is None:
                    self.thread = threading.Thread(target=self._run, name="monitor-camera", daemon=True)
                    self.thread.start()
            result = copy.deepcopy(self.latest)
            failed = self.failed
        if result is None:
            return None
        result["age_seconds"] = max(0, self.clock() - result.pop("observed"))
        result["stale"] = failed or result["age_seconds"] > 5 or self.stop.is_set()
        return result

    def image(self, capture_id):
        with self.lock:
            return self.images.get(capture_id)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=4)

    def _read(self, session):
        started = self.clock()
        with session.get(self.api + "/camera/rgb", headers=self.headers,
                         timeout=(1, 3), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError("Camera unavailable.")
            raw = bytearray()
            for chunk in response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE or self.clock() - started > 6:
                    raise ValueError("Camera response limit exceeded.")
            # The gateway rejects stale sensor frames. Retain its capture time
            # when supplied, and never label local reception as sensor time.
            timestamp = response.headers.get("X-Frame-Timestamp")
            if timestamp:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None or len(timestamp) > 80:
                    raise ValueError("Invalid frame time.")
                timestamp = parsed.astimezone(timezone.utc).isoformat()
            jpeg = camera_jpeg(bytes(raw), response.headers)
        capture_id = "camera-" + uuid.uuid4().hex
        with self.lock:
            self.failed = False
            self.latest = {"capture": capture_id, "saved_at": timestamp,
                           "received_at": utc_now(), "observed": self.clock()}
            self.images[capture_id] = (jpeg, "image/jpeg")
            while len(self.images) > 3:
                self.images.popitem(last=False)

    def _run(self):
        with requests.Session() as session:
            while not self.stop.is_set():
                with self.lock:
                    wanted = self.clock() < self.demand_until
                if not wanted:
                    self.stop.wait(.5)
                    continue
                delay = 2
                try:
                    self._read(session)
                except (requests.RequestException, ValueError, OSError, OverflowError):
                    delay = 12
                    with self.lock:
                        # Keep the last frame, but immediately mark it stale.
                        self.failed = True
                self.stop.wait(delay)


class MonitorAssistant:
    def status(self):
        return {"available": False, "configured": False, "demo": False,
                "settings_editable": False, "remember_supported": False,
                "unavailable_reason": "Monitoring only. Use the main dashboard for Assistant requests and settings."}


class MonitorController:
    read_only = True

    def __init__(self, observer, *, headers=None, demo=False):
        self.observer, self.demo = observer, demo
        self.instance = uuid.uuid4().hex
        self.csrf = secrets.token_urlsafe(32)
        self.camera = CameraReader(observer.url, headers) if not demo else None
        self.telemetry = RobotTelemetry(observer.url, headers) if not demo else None

    def start(self):
        pass

    def close(self):
        if self.camera:
            self.camera.close()
        if self.telemetry:
            self.telemetry.close()

    def observation(self):
        return self.observer.snapshot() if self.demo else self.observer.snapshot(overview=True)

    def active_device(self):
        observation = self.observation()
        section = observation.get("sections", {}).get("control", {})
        data = section.get("data", {})
        fresh = (observation.get("status") in ("live", "degraded", "demo")
                 and section.get("state") == "ok" and not section.get("stale", True))
        owner = data.get("client_id") if fresh and data.get("active") else None
        queue_section = observation.get("sections", {}).get("queue", {})
        queue = queue_section.get("data") if fresh and not queue_section.get("stale", True) else None
        return {"status": ("active" if data.get("active") else "idle") if fresh else "unavailable",
                "device_id": ROLE_IDS.get(owner), "client_id": owner, "label": ROLE_NAMES.get(owner, owner),
                "names": {ROLE_IDS[r]: name for r, name in ROLE_NAMES.items()},
                "queue": ({"state": queue.get("state"), "owner": queue.get("owner"),
                           "label": ROLE_NAMES.get(queue.get("owner"), queue.get("owner"))} if queue else None),
                "updated_at": section.get("updated_at"),
                "message": "Observed gateway lease; the Windows monitor never takes control."}

    def state(self, *, camera_demand=False):
        observation = self.observation()
        sections = observation.get("sections", {})
        health = sections.get("health", {})
        live = health.get("state") == "ok" and not health.get("stale", True)
        queue_section = sections.get("queue", {})
        queue = queue_section.get("data", {}) if live and not queue_section.get("stale", True) else {}
        busy = queue.get("state") == "running"
        steps = queue.get("steps", [])
        total = queue.get("length")
        valid_steps = type(total) is int and 0 < total <= 64 and len(steps) == total
        completed = sum(step.get("state") == "done" for step in steps)
        frame = self.camera.snapshot(demand=camera_demand) if self.camera else None
        inventory = ({"capture": frame["capture"], "saved_at": frame["saved_at"], "cups": [],
                      "image_url": "/api/image/" + frame["capture"]} if frame else None)
        controller = health.get("data", {}).get("robot_controller", {})
        status = {"execution_state": controller.get("rapid_execution_state"),
                  "controller_state": controller.get("controller_state"),
                  "operation_mode": controller.get("operation_mode"), "read_at": health.get("updated_at")}
        return {"instance_id": self.instance, "csrf_token": self.csrf, "revision": time.monotonic_ns(),
                "mode": "demo" if self.demo else "monitor", "read_only": True, "ready": False, "busy": busy,
                "operation": "observed_queue" if busy else None, "scan_usable": False,
                "recovery_required": False, "stop_requested": False, "stop_in_flight": False, "stop": None,
                "llm_enabled": False, "assistant_available": False, "model": None,
                "inventory": inventory, "proposal": None, "error": None,
                "message": observation.get("message"), "motion": None, "batch": None,
                "placement_grid": None, "inference": None,
                "progress": ({"state": queue.get("state"), "step": "Gateway queue", "total": total,
                              "completed": completed} if valid_steps else None),
                "robot_controls": {"status": status if health.get("data") else None, "proposal": None, "home_speed": None},
                "saved_cups": {"enabled": False, "matcher_enabled": False, "cups": []}, "events": [],
                "monitor": {"gateway_status": observation.get("status"), "gateway_online": live,
                            "demo": self.demo, "camera": frame, "queue": queue,
                            "status_stale": not live,
                            "notice": ("Simulated preview. No camera, robot or worker connection. " if self.demo else "This computer reads the gateway independently. ") + "Camera frames, robot pose and queue status can be monitored during Mac disconnects. Cup detections, placement records and Assistant history remain on the worker."}}

    def status(self):
        state = self.state()
        return {key: state[key] for key in ("mode", "read_only", "busy", "operation", "message", "progress", "monitor")}

    def help(self):
        return {"message": "This is the full dashboard in monitoring mode. Pick shows gateway camera frames; 3D and Observer show live robot readings. Use the main dashboard to send commands."}

    def saved_image(self, _cup_id):
        return None

    def image_bytes(self, capture_id):
        return self.camera.image(capture_id) if self.camera else None

    def robot_pose(self):
        if self.demo:
            from robot_client import RobotClient
            return {"source": "simulated", "clock_id": self.instance, "now": time.monotonic(),
                    "arms": {arm: {"joints": list(RobotClient.HOME_JOINTS), "arm_angle": RobotClient.HOME_ARM_ANGLES[arm],
                                   "gripper": 10, "t": time.monotonic()} for arm in ("left", "right")}}
        result = self.telemetry.snapshot()
        # Transport exception text can contain URLs or headers. Never publish it.
        result["error"] = "Robot pose unavailable; retained readings may be stale." if result.get("error") else None
        return {"source": "live", "clock_id": self.instance, **result}


def run_monitor(args, parser):
    if (not (args.gateway or args.demo) or (args.gateway and args.demo)
            or args.live or args.dry_run or args.device_config or args.host not in ("127.0.0.1", "localhost")
            or args.lan_http or args.allowed_origin or args.local_port or args.tls_cert or args.tls_key
            or args.assistant_settings or args.pairing_code_file or args.mac_inference_url or args.isolated_inference):
        parser.error("--monitor needs --gateway (or --demo) and runs locally, alongside your existing controller. Omit controller, device, inference and LAN options.")
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535.")
    try:
        from PIL import Image  # Fail before starting a server with missing camera support.
        token = None if args.demo else diagnostics_token(args.observer_token_file)
        observer = DemoObserver() if args.demo else GatewayObserver(args.gateway, token)
    except ImportError:
        parser.error("Install monitoring dependencies: py -m pip install -r capture_and_detect/requirements-monitor.txt")
    except (OSError, ValueError):
        parser.error("Check the gateway address and diagnostics token file.")
    headers = {"X-Dev-Client": "diagnostics", "Connection": "close"}
    if token:
        headers["Authorization"] = "Bearer " + token
    controller = MonitorController(observer, headers=headers, demo=args.demo)
    from web_app import create_app
    import uvicorn
    app = create_app(controller, port=args.port, assistant=MonitorAssistant(), observer_monitor=observer)
    print(f"Full dashboard monitor: http://localhost:{args.port}", flush=True)
    print("Monitoring only. The Mac and Pi keep their existing controller roles.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
