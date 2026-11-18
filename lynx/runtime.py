"""Lightweight web-controller factory for the deployed native picker runtime.

This module opens no camera, model or network connection at import. The agent
hosting the controller must hold the existing state-directory ``picker.lock``
for the entire controller lifetime; this factory intentionally acquires no lock.
It serves no HTTP or static UI and adds no image, ML or web-framework dependency.
"""
import copy
from pathlib import Path
import sys
import threading


class _WorkerResource:
    """Close the registered native worker once and invalidate cached readiness."""

    def __init__(self, worker, factory):
        self.worker, self.factory = worker, factory
        self._closed = False
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.worker.close()
        finally:
            self.factory._update(state="closed", ready=False, worker_running=False)


class _NativeWebFactory:
    def __init__(self, args):
        self.args = copy.deepcopy(args)
        self._lock = threading.Lock()
        self._started = False
        self._metadata = {"backend": "ncnn-cpp", "ready": False,
                          "worker_running": False, "state": "not_started"}

    def metadata(self):
        """Return JSON-compatible cached status; no file, camera or network IO."""
        with self._lock:
            return copy.deepcopy(self._metadata)

    def _update(self, **fields):
        with self._lock:
            self._metadata.update(copy.deepcopy(fields))

    def __call__(self, controller):
        with self._lock:
            if self._started:
                raise ValueError("This native runtime factory has already been started.")
            self._started = True
            self._metadata["state"] = "starting"

        # Reuse exactly the CLI's shared runtime pieces, imported only when the
        # controller starts its worker. None imports NumPy/OpenCV/FastAPI.
        from camera_client import CameraClient
        from cup_commands import CupSession
        from language_commands import LanguageController
        from local_llm import DisabledIntentClient
        from placing_grid import PlacingGrid
        from recovery_journal import RecoveryJournal
        from robot_client import RobotClient
        from .capture import Calibration, CaptureService, CaptureStore
        from .model import Model
        from .native_perception import NativePerception
        from .picker import read_token
        from .resources import (advise_capture_cache, advise_loaded_python_cache, advise_packaged_native_cache,
                                trim_unused_heap, wait_for_model_headroom)

        args = self.args
        resource = None
        try:
            state = Path(args.state_dir).resolve(strict=True)
            model = Model.load(args.model)
            calibration = Calibration(state / "calibration.json")
            grid = PlacingGrid(state / "placing-grid.json")
            headers = {"Authorization": "Bearer " + read_token(state / "gateway-token")}
            api = getattr(args, "gateway", "http://127.0.0.1:8000").rstrip("/")
            if not api.endswith("/api/v1"):
                api += "/api/v1"
            camera = CameraClient(api, headers, max_dimensions=(1920, 1080))
            store = CaptureStore(state / "captures")
            depth_frames = getattr(args, "depth_frames", 5)
            max_age = getattr(args, "max_result_age", 60.0)
            confidence = getattr(args, "conf", 0.5)
            self._update(gateway_api=api, state_directory=str(state),
                         image_root=str(store.folder),
                         model={"weights": str(model.weights), "sha256": model.sha256,
                                "input_size": model.input_size, "precision": model.precision,
                                "names": list(model.names)},
                         policy={"workflow": "v4_snapshot", "scan_observations": 1,
                                 "verification_frames": 0, "per_cup_capture": False,
                                 "depth_frames": depth_frames,
                                 "camera_z": getattr(args, "camera_z", False),
                                 "confidence_threshold": confidence},
                         memory={"guard_required_mib": 128, "checked": False})
            if sys.platform == "linux":
                controller.event(advise_capture_cache(store.folder, sync=True).summary())
                controller.event(advise_packaged_native_cache().summary())
                controller.event(advise_loaded_python_cache().summary())
                controller.event(trim_unused_heap())
                snapshot = wait_for_model_headroom(128)
                controller.event(snapshot.summary(128))
                self._update(memory={"guard_required_mib": 128, "checked": True,
                    "cgroup_headroom_bytes": snapshot.cgroup_headroom_bytes,
                    "host_available_bytes": snapshot.host_available_bytes})
            controller.event("Starting native cup perception…")
            worker = NativePerception(getattr(args, "worker", "/usr/local/bin/lynx-ncnn-worker"),
                                      model, confidence=confidence,
                                      timeout=getattr(args, "inference_timeout", 30.0))
            resource = _WorkerResource(worker, self)
            controller.attach_resource(resource)
            self._update(worker_running=True)
            captures = CaptureService(camera, worker, calibration, grid, store,
                                      depth_frames=depth_frames, scan_observations=1,
                                      confidence=confidence)
            robot = RobotClient(api, headers, dry_run=not getattr(args, "live", False))
            robot.configure_operator_controls(getattr(args, "controller_profile", None))
            recovery = RecoveryJournal(store.folder / "robot_recovery.json",
                                       legacy_inventory_path=store.folder / "cups.json")

            def capture(purpose):
                controller.event("Capturing and detecting cups on the native worker…" if purpose == "scan"
                                 else "Capturing a requested workspace observation…")
                try:
                    inventory, started = captures.capture(purpose)
                except BaseException as error:
                    # NativePerception closes its process after a failed
                    # inference. Camera/configuration errors can leave that
                    # process available for another explicit scan.
                    process = worker.process
                    running = process.poll() is None and not process.stdin.closed
                    self._update(state="capture_failed", ready=False, worker_running=running,
                                 error_type=type(error).__name__)
                    raise
                advise_capture_cache(store.folder, paths=tuple(
                    inventory["images"][kind] for kind in ("rgb", "depth")))
                self._update(state="ready", ready=True, error_type=None,
                    last_capture={"capture_id": inventory["capture"], "purpose": purpose,
                    "sequence": inventory["frame"]["sequence"],
                    "inference_ms": inventory["model"]["inference_ms"],
                    "frame_count": inventory["placement_observation"]["frame_count"]})
                return inventory, started

            def save(inventory):
                store.save(inventory)
                controller.publish_session()

            session = CupSession(capture, save, robot, calibration.digest,
                                 tag_offset=calibration.offset, speed=getattr(args, "speed", None),
                                 recovery=recovery, placing_grid=grid, verification_frames=1,
                                 strict_single_verification=True, max_result_age=max_age,
                                 camera_z=getattr(args, "camera_z", False))
            language = LanguageController(session, DisabledIntentClient(),
                                          audit_dir=store.folder / "language")
            self._update(state="ready", ready=True, camera=copy.deepcopy(captures.K),
                         calibration_sha256=calibration.digest,
                         motion=session.motion_settings())
            return session, language
        except BaseException as error:
            if resource is not None:
                resource.close()
            self._update(state="failed", ready=False, error_type=type(error).__name__)
            raise


def native_web_factory(args):
    """Build a one-use factory for ``WebController(..., llm_enabled=False)``.

    Arguments match ``lynx.picker``. The returned callable's ``metadata()`` is a
    copied, JSON-compatible status snapshot, including the model, camera, policy
    and capture image root. Native inventory already contains cup pixel/bbox
    coordinates and placement polygons; ``WebController`` registers its raw RGB
    PNG and exposes those coordinates for a browser-drawn overlay.
    """
    return _NativeWebFactory(args)
