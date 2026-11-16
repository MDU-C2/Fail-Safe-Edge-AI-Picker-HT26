"""Exclusive, durable ownership of one full picker runtime.

All managed commands enter through the same admission lock. An idle handoff
retires its generation before acknowledging; a delayed request cannot revive it.
This coordinates participating picker services, not arbitrary gateway clients.
"""
import copy
import hashlib
import json
from pathlib import Path
import threading
import uuid

from cup_inventory import atomic_json
from handover_plan import normalize_handover_offset, motion_speeds, normalize_phase_speeds
from placing_grid import PlacingGrid, _unique_object, _invalid_constant
from recovery_journal import RecoveryJournal
from web_controller import WebController, WebError


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Device state is too large.")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class ExecutionLock:
    """Compatibility handle for single-user runtimes; creates no OS lock file."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class HandoffStore:
    def __init__(self, state_dir, robot_id, *, runtime_paths=None):
        self.root = Path(state_dir).resolve(strict=True)
        paths = runtime_paths or {}
        self.capture_root = Path(paths.get("output", self.root / "captures")).resolve()
        self.calibration_path = Path(paths.get("calibration", self.root / "calibration.json")).resolve()
        self.table_reference_path = Path(paths.get("table_reference", self.root / "placing-grid.table-reference.json")).resolve()
        self.grid = PlacingGrid(Path(paths.get("placing_grid", self.root / "placing-grid.json")).resolve())
        self.recovery_path = self.capture_root / "robot_recovery.json"
        self.recovery = RecoveryJournal(self.recovery_path,
                                       legacy_inventory_path=self.capture_root / "cups.json")
        self.robot_id = robot_id
        self.pending = self.root / "device-handoff-pending.json"
        runtime = {"grid": str(self.grid.path), "output": str(self.capture_root),
                   "calibration": str(self.calibration_path), "table_reference": str(self.table_reference_path)}
        standard = {"grid": str(self.root / "placing-grid.json"), "output": str(self.root / "captures"),
                    "calibration": str(self.root / "calibration.json"),
                    "table_reference": str(self.root / "placing-grid.table-reference.json")}
        self._runtime_digest = digest(runtime) if runtime != standard else None
        self._binding_path = self.root / "device-runtime-binding.json"
        self.handoff_notice = None

    @staticmethod
    def _occupancy_digest(grid):
        return digest([[p["id"], bool(p.get("occupied"))] for p in grid["locations"]])

    def _adopting_runtime(self, grid):
        if self._runtime_digest is None:
            return False
        try:
            binding = read_json(self._binding_path)
        except (OSError, ValueError):
            return True
        return binding != {"schema_version": 1, "paths": self._runtime_digest,
                           "occupancy": self._occupancy_digest(grid)}

    def _bind_runtime(self, grid):
        if self._runtime_digest is not None:
            atomic_json(self._binding_path, {"schema_version": 1, "paths": self._runtime_digest,
                                           "occupancy": self._occupancy_digest(grid)})

    def identity(self):
        value = self.grid._read()
        geometry = {"locations": [{k: p[k] for k in ("id", "x", "y", "z")}
                                  for p in value["locations"]],
                    "cell_size_mm": value.get("cell_size_mm")}
        return {"robot_id": self.robot_id, "geometry": digest(geometry),
                "calibration": hashlib.sha256(self.calibration_path.read_bytes()).hexdigest(),
                "table_reference": hashlib.sha256(self.table_reference_path.read_bytes()).hexdigest()}

    def export(self, session):
        if self.pending.exists():
            raise WebError("An interrupted state transfer needs inspection before switching.")
        grid = self.grid._read()
        if self.recovery.required() or any(p.get("reserved") for p in grid["locations"]):
            raise WebError("Resolve recovery and reserved placement locations before switching devices.")
        # A missing legacy clean journal is represented explicitly in the bundle.
        recovery = read_json(self.recovery_path) if self.recovery_path.exists() else {
            "schema_version": 1, "recovery_required": False}
        motion = {"speed": session.speed, "home_speed": session.home_speed,
                  "camera_z": session.camera_z, "handover_offset": list(session.handover_offset)}
        phases = normalize_phase_speeds(getattr(session, "phase_speeds", None))
        # The existing four-field bundle remains compatible with older
        # services when all stage speeds are at their original defaults.
        if phases != normalize_phase_speeds():
            motion["phase_speeds"] = phases
        # A clean export records the last occupancy managed by this authority.
        # Standalone edits between dashboard runs will require adoption again.
        self._bind_runtime(grid)
        return {"identity": self.identity(), "grid": grid, "recovery": recovery, "motion": motion}

    def install(self, bundle):
        self.check_receive()
        if not isinstance(bundle, dict) or set(bundle) != {"identity", "grid", "recovery", "motion"}:
            raise WebError("Invalid state transfer.", 422)
        if bundle["identity"] != self.identity():
            raise WebError("Devices do not have matching robot, calibration and placement geometry.")
        incoming = bundle["grid"]
        current = self.grid._read()
        if (not isinstance(incoming, dict) or not isinstance(incoming.get("locations"), list)
                or len(incoming["locations"]) != len(current["locations"])):
            raise WebError("Invalid placement transfer.", 422)
        from placing_grid import validate_place
        for old, new in zip(current["locations"], incoming["locations"]):
            validate_place(new)
            if any(old[k] != new[k] for k in ("id", "x", "y", "z")) or new.get("reserved"):
                raise WebError("Placement geometry changed or reservations remain.")
        if incoming.get("cell_size_mm") != current.get("cell_size_mm"):
            raise WebError("Placement cell sizes differ.")
        recovery = bundle["recovery"]
        if (not isinstance(recovery, dict) or recovery.get("schema_version") != 1
                or recovery.get("recovery_required") is not False):
            raise WebError("Unresolved recovery cannot be transferred as a clean handoff.")
        validate_motion(bundle["motion"])
        incoming = copy.deepcopy(incoming)
        preserved = False
        if self._adopting_runtime(current):
            # First attach (or a later standalone edit) must never erase newer
            # occupied records. Geometry and clean recovery were checked above;
            # this union only adds occupied cells and cannot create free space.
            for old, new in zip(current["locations"], incoming["locations"]):
                if old.get("occupied") and not new.get("occupied"):
                    new["occupied"] = True
                    preserved = True
        # Preserve the previous state for diagnosis. A partial transfer keeps a
        # separate marker and recovery guard, never a silently clean startup.
        atomic_json(self.root / "device-state-before-handoff.json", {
            "grid": current, "recovery": read_json(self.recovery_path) if self.recovery_path.exists() else None})
        atomic_json(self.pending, bundle)
        self.recovery.begin({"source": "device_handoff"})
        atomic_json(self.grid.path, incoming)
        atomic_json(self.root / "device-motion.json", bundle["motion"])
        atomic_json(self.recovery_path, recovery)
        self._bind_runtime(incoming)
        self.pending.unlink()
        self.handoff_notice = ("Occupied placement records from both devices were preserved. "
                               "Inspect the workspace before clearing a location." if preserved else None)

    def check_receive(self):
        """Never overwrite unresolved work from a legacy/local controller."""
        reason = self.receive_block_reason()
        if reason:
            raise WebError(reason)

    def receive_block_reason(self):
        if self.pending.exists():
            return "An interrupted device state transfer needs inspection before switching."
        if self.recovery.required():
            return "The target picker has an unfinished operation. Inspect the robot and acknowledge recovery before switching."
        reserved = [f"P{p['id']}" for p in self.grid._read()["locations"] if p.get("reserved")]
        if reserved:
            return ("Unfinished placement reservations: " + ", ".join(reserved)
                    + ". Inspect these locations and resolve their records before switching.")
        return None

    def receive_blocked(self):
        try:
            self.check_receive()
        except WebError:
            return True
        return False


def validate_motion(motion):
    required = {"speed", "home_speed", "camera_z", "handover_offset"}
    if (not isinstance(motion, dict) or not required.issubset(motion)
            or set(motion) - required - {"phase_speeds"}):
        raise WebError("Invalid motion settings transfer.", 422)
    motion_speeds(motion["speed"], motion["home_speed"])
    normalize_phase_speeds(motion.get("phase_speeds"))
    if type(motion["camera_z"]) is not bool:
        raise WebError("Invalid camera-height setting.", 422)
    normalize_handover_offset(motion["handover_offset"])


class DeviceAgent:
    def __init__(self, device_id, factory, store, *, live=False, native=False,
                 controller_class=WebController, gateway_idle=None):
        if device_id not in ("mac", "pi", "lynx"):
            raise ValueError("Unknown execution device.")
        self.device_id, self.factory, self.store = device_id, factory, store
        self.live, self.native = live, native
        self.boot_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self.controller = None
        self._astra = None
        self.transition = False
        self.controller_class = controller_class
        self.gateway_idle = gateway_idle or self._gateway_idle
        self.owner_path = store.root / "device-owner.json"
        self.owner = read_json(self.owner_path) if self.owner_path.exists() else {
            "owner_id": None, "highest_generation": 0, "active_generation": None}
        if (not isinstance(self.owner, dict) or set(self.owner) != {
                "owner_id", "highest_generation", "active_generation"}
                or type(self.owner["highest_generation"]) is not int
                or self.owner["highest_generation"] < 0
                or (self.owner["owner_id"] is not None and (
                    not isinstance(self.owner["owner_id"], str) or len(self.owner["owner_id"]) != 32
                    or any(c not in "0123456789abcdef" for c in self.owner["owner_id"])))
                or (self.owner["active_generation"] is not None and (
                    type(self.owner["active_generation"]) is not int
                    or self.owner["active_generation"] <= 0
                    or self.owner["active_generation"] != self.owner["highest_generation"]))
                or ((self.owner["owner_id"] is None) != (self.owner["highest_generation"] == 0))):
            raise ValueError("Invalid persisted device ownership; inspect state.")

    def _persist(self):
        atomic_json(self.owner_path, self.owner)

    def configure_astra(self, client, settings):
        """Python-only injection; the authenticated device HTTP API has no key route."""
        with self._lock:
            if self.device_id != "mac" or self.native or self.controller is not None or self.transition:
                raise WebError("Configure Astra on the inactive local computer picker before startup.")
            self._astra = (client, settings)

    def _context(self, body):
        if (body.get("boot_id") != self.boot_id or body.get("owner_id") != self.owner["owner_id"]
                or type(body.get("generation")) is not int
                or body["generation"] != self.owner["active_generation"]):
            raise WebError("Device ownership changed. Refresh the dashboard before sending a command.")

    def health(self):
        with self._lock:
            state = self.controller.state() if self.controller else None
            reason = self.store.receive_block_reason() if hasattr(self.store, "receive_block_reason") else None
            return {"device_id": self.device_id, "boot_id": self.boot_id,
                    "identity": self.store.identity(), "owner": copy.deepcopy(self.owner),
                    "mode": "live" if self.live else "dry_run", "native": self.native,
                    "controller_present": state is not None,
                    "ready": bool(state and state["ready"]),
                    "busy": self.transition or bool(state and state["busy"]),
                    "transfer_incomplete": self.store.pending.exists(),
                    "handoff_blocked": self.store.receive_blocked() if hasattr(self.store, "receive_blocked") else False,
                    "handoff_block_reason": reason,
                    "handoff_notice": getattr(self.store, "handoff_notice", None),
                    "features": {"phase_speeds": True},
                    "metadata": self.factory.metadata() if callable(getattr(self.factory, "metadata", None)) else {}}

    def activate(self, body):
        with self._lock:
            if set(body) != {"boot_id", "owner_id", "generation", "handoff"}:
                raise WebError("Invalid activation fields.", 422)
            owner, generation = body["owner_id"], body["generation"]
            if (body["boot_id"] != self.boot_id or not isinstance(owner, str)
                    or len(owner) != 32 or any(c not in "0123456789abcdef" for c in owner)
                    or type(generation) is not int or generation <= 0):
                raise WebError("Invalid or stale activation.", 422)
            if self.owner["owner_id"] not in (None, owner):
                raise WebError("This device belongs to another dashboard authority.")
            if self.controller is not None or self.transition:
                # Only an exact already-active request is harmlessly idempotent.
                self._context(body)
                return self.health()
            if generation <= self.owner["highest_generation"]:
                raise WebError("This activation generation is retired; it cannot be replayed.")
            if self.store.pending.exists():
                raise WebError("An interrupted state transfer needs inspection.")
            if self.owner["active_generation"] is not None and body["handoff"] is not None:
                raise WebError("An earlier active owner must be reconciled before replacing its state.")
            self.owner.update(owner_id=owner, highest_generation=generation, active_generation=generation)
            self._persist()
            if body["handoff"] is not None:
                self.store.install(body["handoff"])

            def factory(controller):
                settings_path = self.store.root / "device-motion.json"
                settings = read_json(settings_path) if settings_path.exists() else None
                if settings is not None:
                    validate_motion(settings)
                    configure = getattr(self.factory, "configure_motion", None)
                    if callable(configure):
                        configure(settings)
                session, language = self.factory(controller)
                if settings is not None:
                    session.speed, session.home_speed = settings["speed"], settings["home_speed"]
                    session.phase_speeds = normalize_phase_speeds(settings.get("phase_speeds"))
                    if session.camera_z != settings["camera_z"]:
                        raise ValueError("Runtime did not construct the transferred camera-height setting.")
                    session.handover_offset = normalize_handover_offset(settings["handover_offset"])
                return session, language

            self.controller = self.controller_class(factory, mode="live" if self.live else "dry_run",
                                                    llm_enabled=False, model="Device picker")
            if self._astra is not None:
                self.controller.configure_astra(*self._astra)
            self.controller.start()
            return self.health()

    def state(self):
        with self._lock:
            health = self.health()
            state = self.controller.state() if self.controller else None
            if state and state.get("inventory") and self.native:
                state["inventory"]["image_overlay"] = "native"
            return {"health": health, "state": state}

    def submit(self, body, *, assistant_request_id=None, assistant_camera=False):
        with self._lock:
            if set(body) != {"boot_id", "owner_id", "generation", "operation", "payload"}:
                raise WebError("Invalid command envelope.", 422)
            self._context(body)
            if self.controller is None:
                raise WebError("The picker must be activated after restart.", 503)
            if self.transition and body["operation"] != "stop":
                raise WebError("A device handoff is in progress.")
            if (assistant_request_id is not None or assistant_camera is not False):
                if self.device_id != "mac" or self._astra is None:
                    raise WebError("Assistant actions require the configured local computer picker.")
                return self.controller.submit(body["operation"], body["payload"],
                    assistant_request_id=assistant_request_id, assistant_camera=assistant_camera)
            # WebController marks work busy synchronously before returning.
            return self.controller.submit(body["operation"], body["payload"])

    def _gateway_idle(self):
        if not self.live:
            return
        health = self.controller.session.robot._get_object("/health")
        robot = health.get("robot_controller", {})
        if (health.get("status") != "ok" or robot.get("ok") is not True
                or robot.get("move_in_progress") is not False
                or health.get("control_lease_active") is not False):
            raise WebError("Gateway has not confirmed idle, unleased robot control. Finish recovery before switching.")

    def _idle(self):
        if self.controller is None:
            raise WebError("Activate this device to reconcile its state before switching.")
        state = self.controller.state()
        if not state["ready"] or state["busy"] or state["recovery_required"] or state["stop_requested"]:
            raise WebError("Wait for the device to finish and resolve recovery before switching.")
        return state

    def deactivate(self, body):
        required = {"boot_id", "owner_id", "generation"}
        if (not isinstance(body, dict) or not required.issubset(body)
                or set(body) - required - {"require_default_phase_speeds"}
                or ("require_default_phase_speeds" in body and body["require_default_phase_speeds"] is not True)):
            raise WebError("Invalid deactivation fields.", 422)

        def check_phase_transfer():
            if (body.get("require_default_phase_speeds") is True
                    and normalize_phase_speeds(getattr(self.controller.session, "phase_speeds", None))
                    != normalize_phase_speeds()):
                raise WebError("Movement speeds changed. Restore stage-speed defaults before switching to this older "
                               "device. Previous device remains selected.")

        with self._lock:
            self._context(body)
            if self.transition:
                raise WebError("A device handoff is already in progress.")
            self._idle()
            # Bind the old-target capability requirement at the source's actual
            # admission boundary, after any previously admitted settings work.
            check_phase_transfer()
            self.transition = True
        try:
            # No command can enter after transition was set. Stop remains
            # available during this bounded, read-only gateway check.
            self.gateway_idle()
            with self._lock:
                self._idle()
                check_phase_transfer()
                bundle = self.store.export(self.controller.session)
                self.controller.language.cancel()
                self.controller.controls.cancel()
                self.controller.session.scan_usable = False
                self.controller.session.inventory = None
                self.controller.publish_session()
                # Persist the exact acknowledgement before retiring ownership.
                # A lost reply can be recovered read-only after restart.
                atomic_json(self.store.root / "device-last-handoff.json", {
                    "owner_id": body["owner_id"], "generation": body["generation"], "handoff": bundle})
                self.owner["active_generation"] = None
                self._persist()
                controller, self.controller = self.controller, None
            controller.close()
            return {"deactivated": True, "handoff": bundle}
        finally:
            with self._lock:
                self.transition = False

    def last_handoff(self):
        with self._lock:
            if self.owner["active_generation"] is not None or self.transition:
                raise WebError("The previous device has not confirmed deactivation.")
            return read_json(self.store.root / "device-last-handoff.json")

    def image(self, capture_id):
        with self._lock:
            registered = self.controller.image(capture_id) if self.controller else None
            if registered:
                path = Path(registered).resolve()
                root = getattr(self.store, "capture_root", self.store.root / "captures").resolve()
                if path.is_relative_to(root) and path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    return path
            return None

    def saved_image(self, cup_id):
        with self._lock:
            return self.controller.saved_image(cup_id) if self.controller is not None and not self.transition else None

    def close(self):
        # Closing the service never silently retires authority or starts another
        # device. Persistent recovery handles interrupted robot work.
        with self._lock:
            controller = self.controller
            self.transition = True
        if controller:
            controller.close()
