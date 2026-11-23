"""Same-origin dashboard facade for manually selected full picker services."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from cup_inventory import atomic_json
from device_agent import ExecutionLock, read_json
from device_service import MAX_DOCUMENT, MAX_IMAGE, read_secret
from local_device import LocalDeviceClient
from web_controller import WebController, WebError

LABELS = {"mac": "MacBook", "pi": "Raspberry Pi", "lynx": "Lynx"}


class DeviceClient:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Device addresses must be configured http(s) origins without credentials or paths.")
        self.url, self.token = url.rstrip("/"), token

    def request(self, path, body=None, *, timeout=3.0):
        import requests
        try:
            # One request, never retry a POST or follow a redirect with credentials.
            with requests.request("GET" if body is None else "POST", self.url + path,
                    headers={"Authorization": "Bearer " + self.token}, json=body,
                    timeout=(min(timeout, 3), timeout), allow_redirects=False, stream=True) as response:
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_DOCUMENT:
                        raise WebError("Device response exceeded its size limit.", 502)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("Not a JSON object")
                if response.status_code != 200:
                    raise WebError(str(result.get("detail", "Device rejected the request."))[:1500],
                                   response.status_code if 400 <= response.status_code < 600 else 502)
                return result
        except WebError:
            raise
        except (requests.RequestException, ValueError, UnicodeError) as error:
            raise WebError("Device connection failed. No command was retried; refresh its status.", 503) from error

    def image(self, capture_id):
        import requests
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", capture_id):
            return None
        try:
            with requests.get(self.url + "/v1/image/" + capture_id,
                    headers={"Authorization": "Bearer " + self.token},
                    timeout=(3, 10), allow_redirects=False, stream=True) as response:
                if response.status_code != 200 or response.headers.get("Content-Type") not in ("image/png", "image/jpeg"):
                    return None
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_IMAGE:
                        return None
                return bytes(raw), response.headers["Content-Type"]
        except requests.RequestException:
            return None


class DeviceDashboard:
    def __init__(self, clients, state_path, *, initial_device="lynx", mode="dry_run", poll_interval=1.0):
        if set(clients) != set(LABELS) or initial_device not in clients or mode not in ("live", "dry_run"):
            raise ValueError("Configure the computer, Raspberry Pi and Lynx, with an initial device and mode.")
        self.clients, self.path, self.mode = clients, Path(state_path), mode
        self.astra_named_matching = isinstance(clients.get("mac"), LocalDeviceClient)
        self.labels = dict(LABELS)
        if self.astra_named_matching:
            self.labels["mac"] = {"darwin": "MacBook", "win32": "Windows PC"}.get(sys.platform, "This PC")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-handoff")
        self._poller = None
        self.poll_interval = poll_interval
        self._cache = {}
        self._busy_switch = False
        self._closed = False
        self._instance = uuid.uuid4().hex
        self._revision = 0
        self._csrf = secrets.token_urlsafe(32)
        self._epoch = uuid.uuid4().hex
        self._message = "Connecting to execution devices…"
        self._error = None
        self._seen = set()
        seed = WebController(lambda _: None, mode=mode, llm_enabled=False)
        self._empty = seed.state()
        seed.close()
        self.process_lock = ExecutionLock(self.path.with_suffix(".lock"))
        try:
            self.record = read_json(self.path) if self.path.exists() else {
                "owner_id": uuid.uuid4().hex, "generation": 0, "phase": "new",
                "device_id": initial_device, "target_id": None, "context": None, "handoff": None}
            if (not isinstance(self.record, dict) or self.record.get("device_id") not in clients
                    or self.record.get("phase") not in ("new", "active", "deactivate_pending", "handoff_ready", "activate_pending")
                    or type(self.record.get("generation")) is not int or self.record["generation"] < 0
                    or not re.fullmatch(r"[0-9a-f]{32}", str(self.record.get("owner_id", "")))):
                raise ValueError("Invalid persisted dashboard ownership. Preserve the file and inspect it.")
        except BaseException:
            self.process_lock.close()
            self._worker.shutdown(wait=True)
            raise

    @classmethod
    def from_config(cls, path, *, mode):
        path = Path(path).resolve()
        config = read_json(path)
        if not isinstance(config, dict) or set(config) != {"schema_version", "initial_device", "state_file", "devices"} or config["schema_version"] != 1:
            raise ValueError("Invalid device configuration.")
        if not isinstance(config["devices"], dict) or set(config["devices"]) != set(LABELS):
            raise ValueError("Configure exactly the computer, Raspberry Pi and Lynx.")
        clients = {}
        try:
            for name, value in config["devices"].items():
                if name == "mac" and isinstance(value, dict) and value.get("backend") == "local":
                    clients[name] = LocalDeviceClient.from_config(value, path.parent, mode=mode)
                    continue
                if not isinstance(value, dict) or set(value) != {"url", "token_file"}:
                    raise ValueError("Invalid configured device.")
                token_path = (path.parent / value["token_file"]).resolve()
                clients[name] = DeviceClient(value["url"], read_secret(token_path))
            dashboard = cls(clients, (path.parent / config["state_file"]).resolve(),
                            initial_device=config["initial_device"], mode=mode)
            dashboard.automatic_failover = bool(mode == "live" and config["devices"]["mac"].get("failover"))
            if dashboard.automatic_failover and dashboard.record["device_id"] != "mac":
                dashboard.close()
                raise ValueError("Select the local computer before enabling automatic failover.")
            return dashboard
        except BaseException:
            for client in clients.values():
                if isinstance(client, LocalDeviceClient):
                    client.close()
            raise

    def configure_astra(self, client, settings):
        with self._lock:
            if self._poller is not None or self._busy_switch or self._closed or not self.astra_named_matching:
                raise WebError("Configure Astra on the combined local dashboard before startup.")
            self.clients["mac"].configure_astra(client, settings)

    def _persist(self):
        atomic_json(self.path, self.record)

    def _invalidate(self):
        self._instance, self._epoch = uuid.uuid4().hex, uuid.uuid4().hex
        self._csrf = secrets.token_urlsafe(32)

    def start(self):
        with self._lock:
            if self._poller:
                return
            self._persist()
            self._poller = threading.Thread(target=self._poll_loop, name="device-status", daemon=True)
            self._poller.start()
            self._busy_switch = True
            self._worker.submit(self._resume)

    def _read(self, device):
        result = self.clients[device].request("/v1/state", timeout=2.0)
        health = result.get("health")
        if (not isinstance(health, dict) or health.get("device_id") != device
                or not re.fullmatch(r"[0-9a-f]{32}", str(health.get("boot_id", "")))
                or not isinstance(health.get("owner"), dict) or not isinstance(health.get("identity"), dict)
                or health.get("mode") != self.mode):
            raise WebError("Device identity or Live/Dry-run mode does not match this dashboard.")
        result["observed_at"] = time.monotonic()
        with self._lock:
            self._cache[device] = result
        return result

    def _poll_loop(self):
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="device-probe") as workers:
            while not self._stop.is_set():
                futures = {name: workers.submit(self._read, name) for name in self.clients}
                for name, future in futures.items():
                    try:
                        future.result()
                    except Exception as error:
                        with self._lock:
                            self._cache[name] = {"error": str(error), "observed_at": time.monotonic()}
                with self._lock:
                    current = self._cache.get(self.record["device_id"], {})
                    health = current.get("health", {})
                    ctx = self.record.get("context") or {}
                    mismatch = self.record["phase"] == "active" and health and health.get("boot_id") != ctx.get("boot_id")
                    if not self._busy_switch and not self._closed and (mismatch or self.record["phase"] != "active"):
                        self._busy_switch = True
                        self._worker.submit(self._resume)
                self._stop.wait(self.poll_interval)

    def _new_context(self, health):
        generation = max(self.record["generation"], health["owner"]["highest_generation"]) + 1
        self.record["generation"] = generation
        return {"boot_id": health["boot_id"], "owner_id": self.record["owner_id"], "generation": generation}

    def _owned(self, health, context):
        return (health.get("boot_id") == context.get("boot_id")
                and health["owner"].get("owner_id") == context.get("owner_id")
                and health["owner"].get("active_generation") == context.get("generation"))

    def _activate(self, target, handoff):
        health = self._read(target)["health"]
        owner = health["owner"]
        if owner.get("owner_id") not in (None, self.record["owner_id"]) or health.get("transfer_incomplete"):
            raise WebError("Target device belongs to another dashboard or has an unfinished state transfer.")
        if handoff is not None and (health["identity"] != handoff["identity"] or owner.get("active_generation") is not None):
            raise WebError("Target geometry differs or the target has an unresolved active owner.")
        if (handoff is not None and "phase_speeds" in handoff.get("motion", {})
                and (health.get("features") or {}).get("phase_speeds") is not True):
            raise WebError("This device service needs the movement-speed update before it can receive custom stage speeds.")
        with self._lock:
            context = self._new_context(health)
            self.record.update(device_id=target, context=context, phase="activate_pending", handoff=handoff)
            self._persist()
            self._invalidate()
        self.clients[target].request("/v1/activate", dict(context, handoff=handoff), timeout=10)
        result = self._read(target)
        if not self._owned(result["health"], context):
            raise WebError("Device activation was not confirmed. Movement remains disabled.")
        with self._lock:
            self.record.update(phase="active", target_id=None, handoff=None)
            self._persist()
            self._message = f"{self.labels[target]} selected. Wait for its model, then scan the workspace."
            if result["health"].get("handoff_notice"):
                self._message += " " + result["health"]["handoff_notice"]
            self._error = None

    def _resume(self):
        try:
            with self._lock:
                phase, device, context = self.record["phase"], self.record["device_id"], copy.deepcopy(self.record["context"])
            data = self._read(device)
            health = data["health"]
            owner = health["owner"]
            if owner.get("owner_id") not in (None, self.record["owner_id"]):
                raise WebError("This picker is owned by another dashboard. No automatic takeover was attempted.")
            if phase == "new":
                # Bootstrap only the explicitly configured initial authority;
                # never choose another host because this one is unavailable.
                if owner.get("active_generation") is not None:
                    raise WebError("Initial device already has an active owner. Restore its dashboard state file.")
                self._activate(device, None)
            elif phase == "active":
                if self._owned(health, context) and health.get("controller_present"):
                    return
                if owner.get("active_generation") != context["generation"]:
                    raise WebError("Persisted ownership differs from the device; automatic takeover blocked.")
                self._activate(device, None)  # New process: same authoritative files, no motion resumed.
            elif phase == "deactivate_pending":
                if owner.get("active_generation") == context["generation"]:
                    if health.get("busy"):
                        raise WebError("Waiting for the previous device to finish its release check.")
                    # Deactivation did not happen. Return to the old owner;
                    # never replay that POST after an uncertain response.
                    with self._lock:
                        self.record.update(phase="active", target_id=None)
                        self._persist()
                        self._error = "Switch was not completed. The previous device remains selected."
                    return
                if owner.get("active_generation") is not None:
                    raise WebError("Previous device ownership is unresolved.")
                ack = self.clients[device].request("/v1/handoff")
                if ack.get("owner_id") != context["owner_id"] or ack.get("generation") != context["generation"]:
                    raise WebError("No matching durable handoff acknowledgement exists.")
                with self._lock:
                    self.record.update(phase="handoff_ready", handoff=ack["handoff"])
                    self._persist()
                self._activate(self.record["target_id"], ack["handoff"])
            elif phase == "handoff_ready":
                self._activate(self.record["target_id"], self.record["handoff"])
            elif phase == "activate_pending":
                if self._owned(health, context) and health.get("controller_present"):
                    with self._lock:
                        self.record.update(phase="active", target_id=None, handoff=None)
                        self._persist()
                        self._message = f"{self.labels[device]} selected. Wait for its model, then scan the workspace."
                        self._error = None
                elif owner.get("active_generation") == context["generation"] and health["boot_id"] != context["boot_id"]:
                    self._activate(device, None)
                elif owner.get("highest_generation", 0) < context["generation"]:
                    # The original activation never reached this boot. Use a
                    # fresh generation, with the persisted exact handoff.
                    self._activate(device, self.record["handoff"])
                else:
                    raise WebError("Activation outcome is unresolved. No movement or takeover was attempted.")
        except Exception as error:
            with self._lock:
                self._error = str(error)
                self._message = "Waiting for confirmed device ownership. No action was retried."
        finally:
            with self._lock:
                self._busy_switch = False

    def _switch(self, target):
        try:
            source = self.record["device_id"]
            old, new = self._read(source), self._read(target)
            if old["health"]["identity"] != new["health"]["identity"]:
                raise WebError("Device calibration, robot or placement geometry differs. Switch blocked.")
            if new["health"]["owner"].get("active_generation") is not None:
                raise WebError("Target device already has an active owner.")
            if new["health"]["owner"].get("owner_id") not in (None, self.record["owner_id"]):
                raise WebError(f"{self.labels[target]} belongs to another dashboard. Previous device remains selected.")
            if new["health"].get("transfer_incomplete") or new["health"].get("handoff_blocked"):
                reason = new["health"].get("handoff_block_reason")
                if not isinstance(reason, str) or not reason.strip():
                    reason = ("An interrupted state transfer needs inspection." if new["health"].get("transfer_incomplete")
                              else "Unfinished recovery or placement reservations need inspection.")
                raise WebError(f"{self.labels[target]}: {reason} Previous device remains selected.")
            from handover_plan import normalize_phase_speeds
            phases = (old.get("state") or {}).get("motion", {}).get("phase_speed_settings")
            if (normalize_phase_speeds(phases) != normalize_phase_speeds()
                    and (new["health"].get("features") or {}).get("phase_speeds") is not True):
                raise WebError(f"{self.labels[target]} needs the movement-speed update. Restore the stage-speed defaults "
                               "or update that service before switching. Previous device remains selected.")
            with self._lock:
                self.record.update(phase="deactivate_pending", target_id=target)
                self._persist()
                context = copy.deepcopy(self.record["context"])
            # New sources recheck this under their admission lock. A settings
            # command already in transit must not retire the source before we
            # discover that an older target cannot receive its updated profile.
            # Legacy sources keep the exact original deactivation envelope.
            if ((old["health"].get("features") or {}).get("phase_speeds") is True
                    and (new["health"].get("features") or {}).get("phase_speeds") is not True):
                context["require_default_phase_speeds"] = True
            ack = self.clients[source].request("/v1/deactivate", context, timeout=10)
            if ack.get("deactivated") is not True or not isinstance(ack.get("handoff"), dict):
                raise WebError("Old device did not acknowledge release. Target remains inactive.")
            with self._lock:
                self.record.update(phase="handoff_ready", handoff=ack["handoff"])
                self._persist()
                self._invalidate()
            self._activate(target, ack["handoff"])
        except Exception as error:
            with self._lock:
                self._error = str(error)
        finally:
            with self._lock:
                self._busy_switch = False

    def _runtime_status(self, device, item):
        """Describe model startup separately from durable execution ownership."""
        health, state = item.get("health") or {}, item.get("state") or {}
        if not health.get("controller_present"):
            return "Standby", None
        if state.get("ready") is True:
            return "Ready", None
        error = state.get("error")
        if not state.get("busy") and isinstance(error, str) and error.strip():
            restart = ("restart this dashboard to reload the local picker"
                       if device == "mac" and self.astra_named_matching
                       else f"restart the {self.labels[device]} picker service")
            return "Startup failed", (f"{self.labels[device]} picker startup failed: {error} "
                f"Fix the cause, then {restart}. Refreshing this page does not reload the model. "
                "Saved placement and recovery records are preserved.")
        return "Starting", f"{self.labels[device]} is starting its picker and model. Wait until it is ready before scanning."

    def state(self):
        # The Mac lives in this process. A fresh browser read after submit must
        # observe its synchronous busy admission or resulting preview, not the
        # remote polling cache from before the request. This does no network I/O.
        with self._lock:
            refresh_mac = self.record["device_id"] == "mac" and self.astra_named_matching and not self._closed
        if refresh_mac:
            try:
                self._read("mac")
            except Exception as error:
                with self._lock:
                    self._cache["mac"] = {"error": str(error), "observed_at": time.monotonic()}
        with self._lock:
            device, context = self.record["device_id"], self.record["context"] or {}
            cached = self._cache.get(device, {})
            health = cached.get("health", {})
            fresh = time.monotonic() - cached.get("observed_at", 0) < 5
            owned = bool(health and self._owned(health, context))
            current = copy.deepcopy(cached.get("state") or self._empty)
            permitted = fresh and owned and self.record["phase"] == "active" and not self._busy_switch and not self._closed
            current.update(instance_id=self._instance, csrf_token=self._csrf, device_epoch=self._epoch)
            if not permitted:
                current.update(ready=False, scan_usable=False, proposal=None)
                current["robot_controls"]["proposal"] = None
            # Switching has no robot job, but prevents every ordinary action.
            current["busy"] = bool(current["busy"] or self._busy_switch)
            if self._error:
                current["error"] = self._error
            if not health:
                current["message"] = cached.get("error", self._message)
            entries = []
            device_message = self._message
            for name in LABELS:
                item = self._cache.get(name, {})
                h = item.get("health")
                available = bool(h and not h.get("transfer_incomplete")
                                 and time.monotonic() - item.get("observed_at", 0) < 5)
                metadata = (h or {}).get("metadata", {})
                model = metadata.get("model")
                if not isinstance(model, str):
                    model = metadata.get("precision") or metadata.get("backend")
                blocked = bool(h and (h.get("transfer_incomplete") or h.get("handoff_blocked")))
                reason = item.get("error") or ((h or {}).get("handoff_block_reason") if blocked else None)
                if blocked and not reason:
                    reason = "Unfinished recovery, reservations or state transfer need inspection."
                runtime_status, runtime_reason = self._runtime_status(name, item)
                if available and runtime_status in ("Starting", "Startup failed"):
                    status, reason = runtime_status, runtime_reason
                else:
                    status = ("Ready" if name == device and permitted and current["ready"]
                              else "Needs inspection" if h and blocked
                              else runtime_status if available else "Unavailable")
                if name == device and permitted:
                    if status in ("Starting", "Startup failed"):
                        device_message = runtime_reason
                        current["message"] = runtime_reason
                        if status == "Startup failed":
                            # Preserve the child's actual startup failure even
                            # if a previous switch attempt also produced an error.
                            current["error"] = (item.get("state") or {}).get("error")
                    elif status == "Ready":
                        device_message = f"{self.labels[name]} selected. Picker and model are ready."
                    if h.get("handoff_notice"):
                        device_message += " " + h["handoff_notice"]
                entries.append({"id": name, "label": self.labels[name], "available": available,
                    "status": status,
                    "model": model, "reason": reason})
            can_switch = (permitted and current["ready"] and not current["busy"]
                          and not current["recovery_required"] and not current["stop_requested"]
                          and not any(p.get("reserved") for p in (current.get("placement_grid") or {}).get("locations", [])))
            if getattr(self, "automatic_failover", False):
                can_switch = False
                device_message = "Automatic failover is enabled: MacBook → Raspberry Pi → Lynx. Start batches here; returning devices regain priority between cups."
            current["devices"] = {"selected_id": self.record.get("target_id") or device,
                "active_id": device if self.record["phase"] not in ("new", "handoff_ready") else None,
                "switching": self._busy_switch or self.record["phase"] != "active",
                "can_switch": bool(can_switch), "entries": entries, "message": device_message}
            current["assistant_available"] = bool(device == "mac" and self.astra_named_matching
                and self.clients["mac"].astra_configured and permitted and current["ready"])
            # Child controllers restart and unavailable snapshots have revision
            # zero. Browser ordering belongs to this dashboard, so neither can
            # make a loss of ownership/capability look like an older snapshot.
            self._revision += 1
            current["revision"] = self._revision
            return current

    def submit(self, operation, payload, *, assistant_request_id=None, assistant_camera=False):
        with self._lock:
            current = self.state()
            if not isinstance(payload, dict) or payload.get("_device_id") != current["devices"]["active_id"] or payload.get("_device_epoch") != self._epoch:
                raise WebError("The selected device changed. Refresh before sending this action.")
            payload = {k: v for k, v in payload.items() if k not in ("_device_id", "_device_epoch")}
            assistant_action = assistant_request_id is not None or assistant_camera is not False
            if assistant_action and (not current["assistant_available"]
                    or operation not in ("scan", "select", "batch", "pick_saved")):
                raise WebError("Assistant actions require the active local computer picker.")
            if operation == "device_select":
                if getattr(self, "automatic_failover", False):
                    raise WebError("Automatic failover chooses the robot worker by priority. Start batches from this computer.")
                if (set(payload) != {"device_id", "request_id"} or payload["device_id"] not in self.clients
                        or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(payload["request_id"]))):
                    raise WebError("Invalid device selection.", 422)
                if payload["request_id"] in self._seen:
                    raise WebError("This device selection was already received.")
                if not self.state()["devices"]["can_switch"]:
                    raise WebError("Wait for the active picker and resolve recovery before switching.")
                if payload["device_id"] == self.record["device_id"]:
                    return {"accepted": True}
                self._seen.add(payload["request_id"])
                self._busy_switch = True
                self._worker.submit(self._switch, payload["device_id"])
                return {"accepted": True}
            # A Stop is sent to the possible active owner, including an
            # activation whose HTTP acknowledgement was lost.
            if operation != "stop" and (self._busy_switch or self.record["phase"] != "active"):
                raise WebError("Wait for confirmed device ownership before sending commands.")
            if self.record["phase"] in ("new", "handoff_ready"):
                raise WebError("No picker currently owns execution; no robot command was sent.")
            context = copy.deepcopy(self.record["context"])
            device = self.record["device_id"]
            if not context:
                raise WebError("No picker has been activated.", 503)
            WebController._validate(operation, payload)
        envelope = dict(context, operation=operation, payload=payload)
        if assistant_action:
            return self.clients[device].submit_assistant(envelope, assistant_request_id=assistant_request_id,
                                                       assistant_camera=assistant_camera)
        result = self.clients[device].request("/v1/command", envelope, timeout=5)
        return result

    def status(self):
        state = self.state()
        keys = ("mode", "ready", "busy", "operation", "scan_usable", "recovery_required",
                "stop_requested", "stop_in_flight", "stop", "progress", "batch", "motion",
                "message", "error", "inference", "devices", "assistant_available")
        result = {key: state.get(key) for key in keys}
        result["capture_id"] = (state.get("inventory") or {}).get("capture")
        result["source"] = "Cached selected-device state; not a live robot status query."
        return result

    def help(self):
        return WebController.help()

    def image(self, capture_id):
        return None

    def image_bytes(self, capture_id):
        with self._lock:
            state = self.state()
            inventory = state.get("inventory") or {}
            if capture_id != inventory.get("image_capture", inventory.get("capture")):
                return None
            device, epoch = self.record["device_id"], self._epoch
        image = self.clients[device].image(capture_id)
        with self._lock:
            return image if epoch == self._epoch else None

    def saved_image(self, cup_id):
        with self._lock:
            if not self.state()["assistant_available"]:
                return None
            epoch = self._epoch
            client = self.clients["mac"]
        data = client.saved_image(cup_id)
        with self._lock:
            return data if epoch == self._epoch and self.state()["assistant_available"] else None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=7)
        self._worker.shutdown(wait=True)
        try:
            if self.astra_named_matching:
                self.clients["mac"].close()
        finally:
            self.process_lock.close()
