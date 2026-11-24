"""In-process local computer endpoint for the existing exclusive picker ownership protocol."""

import copy
from contextlib import ExitStack
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from cup_inventory import finite_number
from device_agent import DeviceAgent, HandoffStore
from device_service import MAX_IMAGE, python_factory
from web_controller import WebError


class LocalDeviceClient:
    """Same handoff protocol as remote clients; credentials never enter an RPC."""

    def __init__(self, agent, *, resources=None):
        if agent.device_id != "mac" or agent.native:
            raise ValueError("Only the Python local picker can run inside the dashboard.")
        self.agent = agent
        self._resources = resources
        self._closed = False
        self.astra_configured = False

    @classmethod
    def from_config(cls, value, directory, *, mode):
        if mode not in ("live", "dry_run"):
            raise ValueError("Invalid local computer execution mode.")
        required = {"backend", "state_dir", "model", "gateway", "robot_id"}
        optional = {"controller_profile", "runtime_paths", "conf", "depth_frames", "max_result_age",
                    "inference_timeout", "threads", "speed", "camera_z", "failover"}
        if (not isinstance(value, dict) or not required.issubset(value)
                or set(value) - required - optional or value["backend"] != "local"):
            raise ValueError("Invalid local computer device configuration.")
        directory = Path(directory).resolve()

        def path(raw, *, exists=False):
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("Local computer paths must be nonempty strings.")
            return (directory / raw).resolve(strict=exists)

        root = path(value["state_dir"], exists=True)
        model = path(value["model"], exists=True)
        if not root.is_dir() or not model.is_file():
            raise ValueError("Local computer state and model paths must exist.")
        gateway = value["gateway"]
        if not isinstance(gateway, str):
            raise ValueError("Invalid local computer gateway origin.")
        parsed = urlsplit(gateway)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Local computer gateway must be an http(s) origin without credentials.")
        if not isinstance(value["robot_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value["robot_id"]):
            raise ValueError("Invalid local computer robot identity.")
        options = {"conf": .5, "depth_frames": 5, "max_result_age": 30.0,
                   "inference_timeout": 30.0, "threads": 3, "speed": None, "camera_z": False}
        options.update({key: value[key] for key in options if key in value})
        if (not finite_number(options["conf"]) or not 0 < options["conf"] < 1
                or type(options["threads"]) is not int or not 1 <= options["threads"] <= 32
                or type(options["depth_frames"]) is not int or not 1 <= options["depth_frames"] <= 9
                or type(options["camera_z"]) is not bool
                or any(not finite_number(options[key]) or not 0 < options[key] <= 300
                       for key in ("max_result_age", "inference_timeout"))):
            raise ValueError("Invalid local computer perception settings.")
        if options["speed"] is not None and (not finite_number(options["speed"]) or not 1 <= options["speed"] <= 1500):
            raise ValueError("Invalid local computer movement speed.")
        paths = {"placing_grid": root / "placing-grid.json", "output": root / "captures",
                 "calibration": root / "calibration.json", "table_reference": root / "placing-grid.table-reference.json"}
        if "runtime_paths" in value:
            runtime = value["runtime_paths"]
            if (not isinstance(runtime, dict) or not {"placing_grid", "output", "calibration"}.issubset(runtime)
                    or set(runtime) - set(paths)):
                raise ValueError("Local runtime_paths requires placing_grid, output and calibration.")
            paths.update({key: path(raw, exists=True) for key, raw in runtime.items()})
            if "table_reference" not in runtime:
                paths["table_reference"] = paths["placing_grid"].with_suffix(".table-reference.json")
        if (not paths["output"].is_dir() or any(not paths[key].is_file()
                for key in ("placing_grid", "calibration", "table_reference"))):
            raise ValueError("Local computer runtime paths must identify the existing picker state.")
        # Camera processing derives its reference filename from the grid; using
        # a different file in handoff identity would assert the wrong geometry.
        if paths["table_reference"] != paths["placing_grid"].with_suffix(".table-reference.json"):
            raise ValueError("The table reference must be paired with the configured placement grid.")
        profile = path(value["controller_profile"]) if "controller_profile" in value else root / "controller-status.json"
        failover_options = {}
        if "failover" in value:
            failover = value["failover"]
            if (not isinstance(failover, dict) or set(failover) != {"coordinator_url", "token_file"}
                    or not isinstance(failover["coordinator_url"], str)
                    or not isinstance(failover["token_file"], str) or not failover["token_file"].strip()):
                raise ValueError("Failover requires coordinator_url and a private token_file.")
            origin = urlsplit(failover["coordinator_url"])
            if (origin.scheme not in ("http", "https") or not origin.hostname or origin.username or origin.password
                    or origin.path not in ("", "/") or origin.query or origin.fragment):
                raise ValueError("Failover coordinator must be an http(s) origin without credentials.")
            token_path = (directory / failover["token_file"]).absolute()
            if token_path.is_symlink() or not token_path.is_file():
                raise ValueError("Failover coordinator token must be an existing regular file.")
            failover_options = {"coordinator_url": failover["coordinator_url"].rstrip("/"),
                                "coordinator_token_file": token_path}
        args = SimpleNamespace(device_id="mac", backend="python", state_dir=root, model=model,
            gateway=gateway.rstrip("/"), robot_id=value["robot_id"], live=mode == "live",
            controller_profile=profile, **options, **failover_options)
        resources = ExitStack()
        try:
            # This single-operator setup uses no OS file locks. Device ownership
            # and interrupted-operation checks remain in the handoff protocol.
            store = HandoffStore(root, args.robot_id, runtime_paths=paths)
            store.identity()
            factory = python_factory(args, runtime_paths=paths, prefer_environment_token=True)
            agent = DeviceAgent("mac", factory, store, live=args.live)
            return cls(agent, resources=resources)
        except BaseException:
            resources.close()
            raise

    def configure_astra(self, client, settings):
        if self._closed:
            raise WebError("The local computer picker is closed.")
        self.agent.configure_astra(client, settings)
        self.astra_configured = True

    def request(self, path, body=None, **kwargs):
        if self._closed:
            raise WebError("The local computer picker is closed.", 503)
        reads = {"/v1/state": self.agent.state, "/v1/health": self.agent.health,
                 "/v1/handoff": self.agent.last_handoff, "/v1/robot-pose": self.agent.robot_pose}
        writes = {"/v1/activate": self.agent.activate, "/v1/deactivate": self.agent.deactivate,
                  "/v1/command": self.agent.submit}
        if body is None and path in reads:
            return copy.deepcopy(reads[path]())
        if isinstance(body, dict) and path in writes:
            return copy.deepcopy(writes[path](copy.deepcopy(body)))
        raise WebError("Unknown local computer operation.", 404)

    def submit_assistant(self, body, *, assistant_request_id=None, assistant_camera=False):
        if self._closed or not self.astra_configured:
            raise WebError("The local computer Assistant is unavailable.", 503)
        return self.agent.submit(copy.deepcopy(body), assistant_request_id=assistant_request_id,
                                 assistant_camera=assistant_camera)

    def image(self, capture_id):
        path = self.agent.image(capture_id)
        if path is None:
            return None
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_IMAGE + 1)
            if len(data) <= MAX_IMAGE:
                return data, "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        except OSError:
            return None
        return None

    def saved_image(self, cup_id):
        return self.agent.saved_image(cup_id) if not self._closed else None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.agent.close()
        finally:
            if self._resources is not None:
                self._resources.close()
