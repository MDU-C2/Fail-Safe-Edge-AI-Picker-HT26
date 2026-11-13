"""Optional inference providers; calibration and task execution stay on this host.

Legacy single-host startup remains available without the new flags. The failover
coordinator runs on the Pi; the Mac runs inference_worker.py, never a second
copy of the robot controller. Importing this module contacts no service.
"""

from contextlib import nullcontext
import copy
import math
import os
from pathlib import Path
import platform
import uuid


def is_pi_host():
    return platform.system() == "Linux" and platform.machine().lower() in ("aarch64", "arm64")


def add_inference_arguments(parser):
    group = parser.add_argument_group("Mac / Pi inference")
    group.add_argument("--mac-inference-url", help="Preferred Mac worker URL; enables automatic local fallback")
    group.add_argument("--isolated-inference", action="store_true", help="Use a bounded local inference process even without a Mac worker")
    group.add_argument("--inference-model", default=str(Path(__file__).parent / "best.pt"))
    group.add_argument("--inference-image-size", type=int, default=640)
    group.add_argument("--inference-threads", type=int, default=3)
    group.add_argument("--mac-timeout", type=float, default=2.0, help="Total remote request budget in seconds; tune from full-path measurements")
    group.add_argument("--pi-timeout", type=float, default=5.0, help="Total local prediction budget in seconds")
    group.add_argument("--mac-probe-interval", type=float, default=3.0)
    group.add_argument("--mac-recovery-successes", type=int, default=3)
    group.add_argument("--inference-startup-timeout", type=float, default=60.0)
    group.add_argument("--inference-events", help="Durable JSONL history; defaults to OUTPUT/inference-events.jsonl")


def validate_inference_arguments(args, parser):
    for name in ("mac_timeout", "pi_timeout", "mac_probe_interval", "inference_startup_timeout"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive and finite")
        if name != "mac_probe_interval" and value > 300:
            parser.error(f"--{name.replace('_', '-')} must not exceed 300 seconds")
    if not 1 <= args.inference_threads <= 32:
        parser.error("--inference-threads must be between 1 and 32")
    if not 1 <= args.mac_recovery_successes <= 100:
        parser.error("--mac-recovery-successes must be between 1 and 100")
    if not 32 <= args.inference_image_size <= 1280 or args.inference_image_size % 32:
        parser.error("--inference-image-size must be a multiple of 32 between 32 and 1280")
    if getattr(args, "demo", False) and (args.mac_inference_url or args.isolated_inference):
        parser.error("Demo mode does not connect to inference workers")
    if hasattr(args, "live") and not args.gateway and (args.mac_inference_url or args.isolated_inference):
        parser.error("Inference workers require --gateway; otherwise the dashboard uses a hardware-free demo")
    live = getattr(args, "live", not getattr(args, "dry_run", True))
    if args.mac_inference_url and live and not is_pi_host():
        parser.error("Run the live fallback coordinator on the Linux ARM64 Pi; run inference_worker.py on the Mac. Use dry-run for local development.")


def create_detector(args, calibration, intrinsics, *, on_update=None, register_resource=None):
    """Return (detector, weights), loading optional heavy dependencies lazily."""
    from capture_and_detect import Detector, load_model

    if not (getattr(args, "mac_inference_url", None) or getattr(args, "isolated_inference", False)):
        model, weights = load_model()
        return Detector(model, calibration, intrinsics, args.conf), weights

    from inference_backends import ProcessBackend, RemoteBackend
    from inference_protocol import make_settings, model_sha256, validate_response
    from inference_supervisor import InferenceSupervisor

    weights = Path(args.inference_model).resolve()
    digest = model_sha256(weights)
    settings = make_settings(conf=args.conf, imgsz=args.inference_image_size)
    remote = None
    if args.mac_inference_url:
        token = os.environ.get("INFERENCE_TOKEN", "")
        if not token:
            raise ValueError("Set INFERENCE_TOKEN to the shared inference-worker token; it must be separate from GATEWAY_TOKEN.")
        remote = RemoteBackend(args.mac_inference_url, token)
    local = ProcessBackend(weights, settings, threads=args.inference_threads,
                           startup_timeout_s=args.inference_startup_timeout)
    supervisor = None
    try:
        supervisor = InferenceSupervisor(local, remote, validate_response=validate_response,
            remote_timeout_s=args.mac_timeout, local_timeout_s=args.pi_timeout,
            probe_interval_s=args.mac_probe_interval, recovery_successes=args.mac_recovery_successes,
            event_path=args.inference_events or str(Path(args.output) / "inference-events.jsonl"),
            on_update=on_update, local_provider="pi" if is_pi_host() else "local")
        if register_resource is not None:
            register_resource(supervisor)
        # Warm local inference before reporting readiness: fallback must not
        # depend on downloading weights or loading a model during a pick.
        local.start()
        supervisor.start()
        return ProviderDetector(supervisor, digest, settings, calibration, intrinsics), weights
    except BaseException:
        if supervisor is not None:
            supervisor.close()
        else:
            local.close()
            if remote is not None:
                remote.close()
        raise


def create_robot(args, api, headers):
    """Read-only capability check before enabling the new live coordinator."""
    from robot_client import RobotClient, RobotCommandError
    live = getattr(args, "live", not getattr(args, "dry_run", True))
    require_session = bool(getattr(args, "mac_inference_url", None)) and live
    robot = RobotClient(api, headers, dry_run=not live, require_session=require_session)
    if require_session:
        control = robot._get_object("/control")
        if control.get("session_required") is not True or control.get("watchdog_enabled") is not True:
            raise RobotCommandError("Live fallback requires the updated gateway with GATEWAY_CONTROL_REQUIRE_SESSION=true. Dry-run remains available.")
    if getattr(args, "controller_profile", None) is not None:
        robot.configure_operator_controls(args.controller_profile)
    return robot


class ProviderDetector:
    """Apply trusted local geometry to an exact-frame segmentation result."""

    def __init__(self, supervisor, weights_digest, settings, calibration, intrinsics):
        self.supervisor, self.weights_digest = supervisor, weights_digest
        self.settings = copy.deepcopy(settings)
        self.calibration, self.K = calibration, intrinsics
        self.conf = settings["conf"]
        self.inference_metadata = None

    def observation_group(self):
        return self.supervisor.observation_group()

    @property
    def generation(self):
        return self.supervisor.generation

    def close(self):
        self.supervisor.close()

    def analyse(self, rgb, depth):
        return self.analyse_observation(rgb, depth, {})

    def analyse_observation(self, rgb, depth, frame):
        import cv2
        import numpy as np
        from camera_client import CameraError
        from capture_and_detect import Detector, mark_cup_bases
        from inference_protocol import make_request, decode_detections

        observation_id = f"{uuid.uuid4().hex}:{frame.get('sequence', 'image')}"
        request = make_request(rgb, model_sha256=self.weights_digest, settings=self.settings,
                               observation_id=observation_id)
        try:
            response = self.supervisor.predict(request)
            detections = decode_detections(response)
        except Exception as error:
            raise CameraError(f"No usable inference result: {error}") from error
        self.inference_metadata = copy.deepcopy(response.get("_inference"))
        annotated = rgb.copy()
        objects, boxes = [], []
        colors = {"CUP": (180, 160, 40), "CUP BASE": (80, 200, 100), "CUP HANDLE": (80, 140, 240)}
        for index, detection in enumerate(detections):
            mask = detection["mask"]
            if mask.shape != depth.shape:
                raise CameraError("Inference mask does not match the retained depth observation")
            if not mask.any():
                continue
            name, confidence = detection["class_name"], detection["confidence"]
            box = detection["bbox_xyxy"]
            obj = Detector.describe(self, name, confidence, mask, depth)
            obj.update(detection_id=index, bbox_xyxy=[round(float(v), 1) for v in box])
            objects.append(obj)
            boxes.append(box)
            color = colors.get(name, (120, 180, 120))
            annotated[mask] = (annotated[mask].astype(np.float32) * .7 + np.array(color) * .3).astype(np.uint8)
            x1, y1, x2, y2 = (int(v) for v in box)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, f"{name} {confidence:.2f}", (x1, max(18, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1)
            cv2.drawMarker(annotated, (int(obj["x_px"]), int(obj["y_px"])), (0, 0, 255),
                           cv2.MARKER_CROSS, 25, 3)
        mark_cup_bases(objects, boxes)
        return objects, annotated


def observation_group(detector):
    group = getattr(detector, "observation_group", None)
    return group() if callable(group) else nullcontext()
