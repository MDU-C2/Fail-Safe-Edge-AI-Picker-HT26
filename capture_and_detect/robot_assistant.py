"""Cloud reasoning beside the picker; only existing, confirmed actions can move it."""

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from assistant_api import AssistantError, AssistantSettings, AstraClient
from named_cups import _name
from web_controller import WebError


class RobotAssistant:
    def __init__(self, controller, *, image_root=None, settings=None, client=None, clock=time.monotonic):
        self.controller = controller
        self.root = Path(image_root).resolve() if image_root else None
        self.settings = settings if settings is not None else AssistantSettings()
        self.client = client if client is not None else AstraClient(self.settings)
        self.clock = clock
        self._lock = threading.Lock()
        self._image_busy = threading.Lock()
        self.image_timeout = 8.0
        self.scan_timeout = 45.0
        self.scene_max_age = 30.0
        self._proposal = None

    def status(self):
        state = self.controller.state()
        return dict(self.settings.status(), demo=state.get("mode") == "demo",
                    context_label=self._label(state), available=self._available(state),
                    unavailable_reason=None if self._available(state) else self._unavailable_reason())

    @staticmethod
    def _available(state):
        devices = state.get("devices")
        # Only the owned, in-process Mac controller shares Astra credentials.
        # A remote service named "mac" does not gain this capability by its name.
        return not devices or (state.get("assistant_available") is True
                               and devices.get("active_id") == "mac" and not devices.get("switching"))

    @staticmethod
    def _unavailable_reason():
        return "Assistant features run on the MacBook controller only. Open the MacBook dashboard to use them."

    @staticmethod
    def _label(state):
        if state.get("mode") == "demo":
            return "Illustrated sample scene — no gateway or robot connected"
        active = (state.get("devices") or {}).get("active_id") or "MacBook"
        capture = (state.get("inventory") or {}).get("capture")
        return f"{active} · saved scan {capture}" if capture else f"{active} · no scan yet"

    @staticmethod
    def _binding(state):
        # Bind to the entire published observation and execution context, not a
        # model-generated ID. Never include secrets in a cloud request.
        fields = {key: state.get(key) for key in (
            "instance_id", "device_epoch", "assistant_available", "mode", "inventory", "placement_grid", "motion",
            "ready", "busy", "scan_usable", "stop_requested", "recovery_required", "saved_cups")}
        fields["device"] = (state.get("devices") or {}).get("active_id")
        fields["switching"] = (state.get("devices") or {}).get("switching")
        fields["proposal"] = (state.get("proposal") or {}).get("token")
        fields["control"] = ((state.get("robot_controls") or {}).get("proposal") or {}).get("token")
        return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _device_context(state):
        return {"device_id": (state.get("devices") or {}).get("active_id"),
                "device_epoch": state.get("device_epoch")}

    def _check_expected_context(self, state, expected_context):
        if expected_context is not None and expected_context != self._device_context(state):
            raise WebError("The selected device changed. Wait for the dashboard to refresh and ask again.", 409)

    @staticmethod
    def _camera_command(text):
        if RobotAssistant._declines_camera(text):
            return False
        request = " ".join(text.strip().casefold().split()).strip(".!?")
        request = re.sub(r"^please\s+", "", request)
        request = re.sub(r"^(?:can|could|would|will) you\s+(?:please\s+)?", "", request)
        request = re.sub(r"(?:\s+(?:now|again|please|for me))+$", "", request)
        return bool(re.fullmatch(
            r"(?:scan(?: (?:again|the (?:table|workspace|scene|cups)))?"
            r"|(?:take|capture)(?: (?:a|another|a new|a fresh))? (?:photo|picture|image)"
            r"(?: of (?:the )?(?:table|workspace|scene|cups))?"
            r"|look at (?:the )?(?:table|workspace|scene|cups))", request))

    @staticmethod
    def _declines_camera(text):
        return bool(re.search(
            r"\b(?:do not|don't|don’t|never|not|without)\s+(?:to\s+)?"
            r"(?:(?:please|ever|just|automatically)\s+)?(?:take|taking|capture|capturing|scan|scanning)\b"
            r"|\b(?:do not|don't|don’t|never|not)\s+(?:use|using)\s+(?:the\s+)?camera\b"
            r"|\bno (?:new |fresh )?(?:photo|picture|image|scan)\b"
            r"|\bwithout\s+(?:(?:a|any|another|new|fresh)\s+)*(?:photo|picture|image|scan|capture)\b",
            text, re.I))

    @staticmethod
    def _needs_scene(text):
        if RobotAssistant._declines_camera(text):
            return False
        return bool(re.search(r"\b(?:cups?|table|workspace|scene|images?|photos?|pictures?|camera|grid|"
                              r"placement|objects?|see|look|scan|detect|find|pick|sort|group|colou?r|"
                              r"leftmost|rightmost|porcelain|paper)\b"
                              r"|\b(?:what|how many)\b.{0,30}\b(?:here|there|in front)\b", text, re.I))

    @staticmethod
    def _numbered_reference(text):
        number = r"(?:[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        return bool(re.search(rf"\b(?:cup|number|pick(?: up)?|select|save|remember)\s+#?\s*{number}\b"
                              r"|\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth) cup\b", text, re.I))

    def _scene_fresh(self, state):
        inventory = state.get("inventory") or {}
        if not state.get("scan_usable") or not inventory.get("capture"):
            return False
        try:
            stamp = datetime.fromisoformat(inventory["saved_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return 0 <= age <= self.scene_max_age
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    def _scan_scene(self, state):
        if (not self._can_prepare(state, require_scan=False) or state.get("proposal")
                or ((state.get("robot_controls") or {}).get("proposal"))):
            raise WebError("A fresh photo is needed. Finish or cancel the current preview and wait for the picker "
                           "to be ready; camera refresh never clears a preview or recovery state.", 409)
        context = self._device_context(state)
        instance = state.get("instance_id")
        old_capture = (state.get("inventory") or {}).get("capture")
        payload = {"request_id": uuid.uuid4().hex}
        if state.get("devices"):
            payload.update(_device_id=context["device_id"], _device_epoch=context["device_epoch"])
        accepted = self.controller.submit("scan", payload, assistant_camera=True)
        if not isinstance(accepted, dict) or accepted.get("accepted") is not True:
            raise WebError("The camera refresh was not accepted. No assistant action was prepared.", 409)
        deadline = time.monotonic() + self.scan_timeout
        while True:
            current = self.controller.state()
            if (self._device_context(current) != context or current.get("instance_id") != instance
                    or not self._available(current)):
                raise WebError("The device changed while taking the photo. Ask again on the selected MacBook.", 409)
            if current.get("stop_requested") or current.get("recovery_required"):
                raise WebError("The picker stopped or requires inspection. The camera result was discarded.", 409)
            if current.get("proposal") or ((current.get("robot_controls") or {}).get("proposal")):
                raise WebError("Another preview appeared while taking the photo. Review it before asking again.", 409)
            if not current.get("busy"):
                capture = (current.get("inventory") or {}).get("capture")
                if (not current.get("ready") or current.get("error") or not capture or capture == old_capture
                        or not self._scene_fresh(current)):
                    raise WebError("The camera refresh did not produce a usable new photo. Check the camera connection "
                                   "and try again; no assistant action was prepared.", 503)
                return current
            if current.get("operation") != "scan":
                raise WebError("The picker started another operation while taking the photo. Ask again when it finishes.", 409)
            if time.monotonic() >= deadline:
                raise WebError("The camera refresh took too long. It may finish in the background; no assistant action "
                               "will start automatically. Ask again once the picker is ready.", 504)
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def _camera_reply(self, state, message):
        return dict(message=message, action="answer", cup_ids=[], groups=[], cup_name=None,
                    saved_cup_id=None, proposal_id=None, mode="camera_command", camera_refreshed=True,
                    capture_id=(state.get("inventory") or {}).get("capture"), context_label=self._label(state))

    @staticmethod
    def _library(state):
        library = state.get("saved_cups") or {}
        cups = []
        for record in library.get("cups", [])[:32]:
            if (isinstance(record, dict) and isinstance(record.get("id"), str)
                    and re.fullmatch(r"[0-9a-f]{32}", record["id"])):
                try:
                    name = _name(record.get("name"))
                except ValueError:
                    continue
                cups.append({"id": record["id"], "name": name})
        return {"enabled": library.get("enabled") is True,
                "matcher_enabled": library.get("matcher_enabled") is True, "cups": cups}

    @staticmethod
    def _scene(state):
        inventory = state.get("inventory") or {}
        cups = [{key: copy.deepcopy(cup.get(key)) for key in (
            "id", "confidence", "pixel", "bbox_xyxy", "selectable", "pick_available", "can_save", "blocked_reason", "status")}
            for cup in inventory.get("cups", [])[:100]]
        if state.get("mode") == "demo":
            for cup in cups:
                cup["sample_colour"] = {1: "teal", 2: "white", 3: "orange"}.get(cup["id"])
        return {"capture_id": inventory.get("capture"), "saved_at": inventory.get("saved_at"),
                "source": "synthetic demonstration" if state.get("mode") == "demo" else "saved camera observation",
                "cups": cups, "saved_cups": RobotAssistant._library(state),
                "placement_grid": copy.deepcopy(state.get("placement_grid")),
                "motion_settings": copy.deepcopy(state.get("motion")),
                "ready": state.get("ready"), "scan_usable": state.get("scan_usable"),
                "busy": state.get("busy"), "recovery_required": state.get("recovery_required"),
                "stop_requested": state.get("stop_requested"),
                "capabilities": "Select one cup for a reviewed pick, or pick all available cups. "
                "Save a named cup reference or select an existing saved reference for a fresh visual match. "
                "Saved cup IDs identify reference photos, never current detections or coordinates. "
                "Sorting is a grouping suggestion only; placement follows the existing grid order. "
                "Movement speeds are read-only here; the operator changes them in Settings. "
                "Never claim a new scan, motion, home, grip, stop, or recovery was executed."}

    def _image(self, state):
        capture = (state.get("inventory") or {}).get("capture")
        if not capture or state.get("mode") == "demo":
            return None
        remote = getattr(self.controller, "image_bytes", None)
        if callable(remote):
            # Socket read timeouts alone do not bound a trickling response.
            # Keep at most one image fetch alive and discard a late result.
            if not self._image_busy.acquire(blocking=False):
                raise WebError("The previous device image is still loading. Try again when it finishes.", 409)
            outcome = queue.Queue(maxsize=1)
            def fetch():
                try:
                    value = remote(capture)
                except Exception:
                    value = None
                finally:
                    self._image_busy.release()
                outcome.put(value)
            try:
                threading.Thread(target=fetch, name="assistant-scene-image", daemon=True).start()
            except Exception:
                self._image_busy.release()
                raise WebError("Could not load the saved device image.", 503) from None
            try:
                value = outcome.get(timeout=self.image_timeout)
            except queue.Empty:
                raise WebError("The saved device image took too long to load. No OpenAI request was sent.", 504) from None
            if value and value[1] in ("image/png", "image/jpeg") and len(value[0]) <= 4 * 1024 * 1024:
                return value
            return None
        registered = self.controller.image(capture)
        if registered and self.root:
            path = Path(registered).resolve()
            if path.is_relative_to(self.root) and path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png"):
                with path.open("rb") as handle:
                    data = handle.read(4 * 1024 * 1024 + 1)
                if len(data) <= 4 * 1024 * 1024:
                    return data, "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return None

    def _enter(self):
        if not self._lock.acquire(blocking=False):
            raise WebError("An assistant request is already running. Stop remains available in the dashboard.", 409)

    def update(self, payload):
        self._enter()
        try:
            self.settings.update(payload)
            self._proposal = None
            return self.status()
        finally:
            self._lock.release()

    def forget(self):
        self._enter()
        try:
            self.settings.clear()
            self._proposal = None
            return self.status()
        finally:
            self._lock.release()

    def test(self):
        self._enter()
        try:
            self._check_matching_priority()
            return self.client.test_connection()
        finally:
            self._lock.release()

    def _check_matching_priority(self):
        state = self.controller.state()
        if state.get("busy") and state.get("operation") in ("pick_saved", "confirm", "command"):
            raise WebError("The picker is checking a cup. Wait for it to finish before starting another Astra request.", 409)

    @staticmethod
    def _named_command(text, scene):
        """Exact library commands need no paid interpretation or conversation memory."""
        request = " ".join(text.strip().split())
        result = dict(message="", action="answer", cup_ids=[], groups=[], cup_name=None, saved_cup_id=None)
        library = scene["saved_cups"]
        if request.casefold() in ("saved cups", "list saved cups", "show saved cups"):
            result["message"] = ("Saved cups: " + ", ".join(cup["name"] for cup in library["cups"])
                                 if library["cups"] else "No cups are saved on the selected picker yet.")
            return result
        match = re.fullmatch(r"(?:save|remember) (?:cup )?([1-9][0-9]*) as (.+)", request, re.I)
        if match:
            try:
                name = _name(match[2])
            except ValueError as error:
                result["message"] = str(error)
                return result
            cup_id = int(match[1])
            if cup_id not in {cup["id"] for cup in scene["cups"]}:
                result["message"] = "That cup is not in the current scan. Scan first and use its displayed number."
                return result
            result.update(action="save_cup", cup_ids=[cup_id], cup_name=name,
                          message=f"Save cup {cup_id} as {name}? This stores a reference photo and sends no robot movement.")
            return result
        match = re.fullmatch(r"pick(?: up)? (?:(saved) )?(.+)", request, re.I)
        if match:
            # Numeric and batch selections keep their ordinary meaning even if
            # a reference has that name. An explicit "pick saved …" can find it.
            if not match[1] and re.fullmatch(r"(?:cup )?[0-9]+|all(?: available)?(?: cups)?", match[2], re.I):
                return None
            try:
                name = _name(match[2]).casefold()
            except ValueError:
                name = None
            references = [cup for cup in library["cups"] if cup["name"].casefold() == name]
            if not references and match[1] and match[2].casefold().startswith("cup "):
                try:
                    name = _name(match[2][4:]).casefold()
                except ValueError:
                    name = None
                references = [cup for cup in library["cups"] if cup["name"].casefold() == name]
            if len(references) == 1:
                result.update(action="pick_saved", saved_cup_id=references[0]["id"],
                    message=f"Find {references[0]['name']} in a fresh scan and prepare a pick preview. "
                            "Check the matched cup before confirming movement.")
                return result
            if match[1] or "'s " in match[2] or "’s " in match[2]:
                result["message"] = ("No unique saved reference matches that name on the selected picker. "
                                     "Scan and use ‘Remember cup 2 as Kasun’s cup’ with the correct cup number first.")
                return result
        return None

    @staticmethod
    def _demo(text, scene):
        result = {"message": "", "action": "answer", "cup_ids": [], "groups": [],
                  "cup_name": None, "saved_cup_id": None}
        cups = scene["cups"]
        selectable = [cup["id"] for cup in cups if cup.get("pick_available")]
        lower = text.lower()
        if re.search(r"\b(don['’]?t|do not|never|not|if|would|could|hypothetical|stop|halt|cancel)\b", lower):
            result["message"] = "No pick selected. Use a direct request such as ‘pick cup two’. Use the dedicated Stop control to stop the robot."
        elif not cups:
            result["message"] = "Click Scan in Pick cups to load the illustrated sample scene, then ask again."
        elif re.search(r"\b(pick|select|transfer)\b", lower):
            cup_id = None
            match = re.search(r"\bcup\s+(one|two|three|[0-9]+)\b", lower)
            if match:
                cup_id = {"one": 1, "two": 2, "three": 3}.get(match[1])
                if cup_id is None:
                    cup_id = int(match[1])
            elif "leftmost" in lower:
                cup_id = min(cups, key=lambda cup: cup["pixel"]["u"])["id"]
            elif "rightmost" in lower:
                cup_id = max(cups, key=lambda cup: cup["pixel"]["u"])["id"]
            else:
                matches = [cup["id"] for cup in cups if cup.get("sample_colour") in lower]
                if len(matches) == 1:
                    cup_id = matches[0]
            if re.search(r"\ball\b", lower) and selectable:
                result.update(message="I can prepare a preview of all available sample cups.", action="batch", cup_ids=selectable)
            elif cup_id in selectable:
                result.update(message=f"Sample cup {cup_id} matches. Prepare a preview to review the pick.", action="select", cup_ids=[cup_id])
            elif cup_id is not None:
                result["message"] = f"Cup {cup_id} is absent or unavailable for picking. No preview was prepared."
            else:
                result["message"] = "Try ‘pick the white cup’, ‘pick cup one’, or ‘pick all available cups’."
        elif "sort" in lower or "group" in lower:
            result.update(message="The sample cups are teal, white and orange. These are suggested groups; the robot still places into its existing grid order.",
                          action="sort", cup_ids=[cup["id"] for cup in cups],
                          groups=[{"label": cup["sample_colour"], "cup_ids": [cup["id"]]} for cup in cups])
        else:
            result["message"] = (f"The illustration has {len(cups)} cups: teal 1, white 2 and orange 3. "
                                 "Cups 1 and 2 can be previewed. Cup 3 has no paired base detection, so it is blocked. "
                                 "Home, grip and recovery remain in Robot controls.")
        result["message"] = "Scripted demo (not GPT): " + result["message"]
        return result

    def ask(self, text, *, expected_context=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            raise WebError("Enter a request between 1 and 2,000 characters.", 422)
        self._enter()
        try:
            self._proposal = None
            state = self.controller.state()
            self._check_expected_context(state, expected_context)
            if not self._available(state):
                raise WebError(self._unavailable_reason(), 403)
            scene = self._scene(state)
            configured = self.settings.status()["configured"]
            demo = not configured and state.get("mode") == "demo"
            result = self._named_command(text, scene)
            named_command = result is not None
            refreshed = False
            camera_command = self._camera_command(text)
            if camera_command:
                state = self._scan_scene(state)
                return self._camera_reply(state, "A new workspace photo is ready in Pick cups. No movement was requested.")
            if not named_command:
                if not configured and not demo:
                    raise AssistantError("Save an OpenAI API key in Settings first.")
                if self._needs_scene(text) and not self._scene_fresh(state):
                    state = self._scan_scene(state)
                    refreshed = True
                    if self._numbered_reference(text):
                        return self._camera_reply(state, "I took a new photo. Cup numbers can change between photos. "
                            "Check the new image and repeat your request with its current cup number; nothing was selected.")
                    scene = self._scene(state)
                if not demo:
                    self._check_matching_priority()
                binding = self._binding(state)
                image = None if demo else self._image(state)
                scene["image_attached"] = image is not None
                result = self._demo(text, scene) if demo else self.client.interpret(text, scene, image=image)
            else:
                binding = self._binding(state)
            result = copy.deepcopy(result)
            result.update(proposal_id=None, mode="saved_cup_command" if named_command else "scripted_demo" if demo else "gpt",
                          context_label=self._label(state), camera_refreshed=refreshed,
                          capture_id=(state.get("inventory") or {}).get("capture"))
            if self._binding(self.controller.state()) != binding:
                result.update(action="answer", cup_ids=[], groups=[], cup_name=None, saved_cup_id=None,
                              message="The scan, device or robot state changed while answering. Ask again using the current scene.")
                return result
            self._validate_result(result, scene)
            if result["action"] == "sort":
                result["message"] += " Grouping is advice only; this version uses the existing placement order."
            elif result["action"] in ("save_cup", "pick_saved"):
                library = scene["saved_cups"]
                reason = None
                if not library["enabled"]:
                    reason = "Saved cup references are unavailable on the selected picker."
                elif result["action"] == "pick_saved" and not library["matcher_enabled"]:
                    reason = "Astra photo matching is not enabled. Save an OpenAI API key in Settings " \
                             "on the MacBook before finding a saved cup."
                elif result["action"] == "save_cup" and any(
                        cup["name"].casefold() == _name(result["cup_name"]).casefold() for cup in library["cups"]):
                    reason = "That name is already saved. Choose another name, or use Forget in Saved cup references first."
                elif result["action"] == "save_cup" and not any(
                        cup["id"] == result["cup_ids"][0] and cup.get("can_save") is True for cup in scene["cups"]):
                    reason = "This detection has no full cup reference image to save. Scan again and select a complete cup."
                elif not self._can_prepare(state, require_scan=result["action"] != "pick_saved"):
                    reason = "Wait for the picker to be ready and check its scan and recovery status in Pick cups."
                if reason:
                    result.update(action="answer", cup_ids=[], groups=[], cup_name=None, saved_cup_id=None,
                                  message=reason)
                else:
                    token = uuid.uuid4().hex
                    self._proposal = {"id": token, "binding": binding, "created": self.clock(),
                                      "action": result["action"], "ids": result["cup_ids"],
                                      "cup_name": result["cup_name"], "saved_cup_id": result["saved_cup_id"]}
                    result["proposal_id"] = token
            elif result["action"] in ("select", "batch"):
                available = {cup["id"] for cup in scene["cups"] if cup.get("pick_available")}
                eligible = self._can_prepare(state) and set(result["cup_ids"]).issubset(available)
                if result["action"] == "batch" and set(result["cup_ids"]) != available:
                    eligible = False
                    result["message"] += " This dashboard supports one selected cup or all available cups; ask for one of those."
                if eligible:
                    token = uuid.uuid4().hex
                    self._proposal = {"id": token, "binding": binding, "created": self.clock(),
                                      "action": result["action"], "ids": result["cup_ids"], "state": state}
                    result["proposal_id"] = token
                else:
                    result["message"] += " No actionable preview is available. Check the current scan and robot status in Pick cups."
            return result
        finally:
            self._lock.release()

    @staticmethod
    def _validate_result(result, scene):
        ids = result.get("cup_ids")
        known = {cup["id"] for cup in scene["cups"]}
        if (result.get("action") not in ("answer", "select", "batch", "sort", "save_cup", "pick_saved") or not isinstance(ids, list)
                or any(type(value) is not int or value not in known for value in ids)
                or len(ids) != len(set(ids)) or not isinstance(result.get("message"), str)
                or (result["action"] in ("select", "save_cup") and len(ids) != 1)
                or (result["action"] == "batch" and not ids)):
            raise WebError("The assistant did not return a valid selection from this scan. Ask again.", 422)
        action = result["action"]
        if action == "save_cup":
            try:
                result["cup_name"] = _name(result.get("cup_name"))
            except ValueError:
                raise WebError("The assistant did not return a valid cup name. Ask again.", 422) from None
            valid = result.get("saved_cup_id") is None and not result.get("groups")
        elif action == "pick_saved":
            valid = (not ids and not result.get("groups") and result.get("cup_name") is None
                     and result.get("saved_cup_id") in {cup["id"] for cup in scene["saved_cups"]["cups"]})
        else:
            valid = result.get("cup_name") is None and result.get("saved_cup_id") is None
        if not valid:
            raise WebError("The assistant did not return a valid saved-cup request. Ask again.", 422)

    @staticmethod
    def _can_prepare(state, *, require_scan=True):
        return (state.get("ready") and (state.get("scan_usable") or not require_scan) and not state.get("busy")
                and not state.get("stop_requested") and not state.get("recovery_required")
                and not (state.get("devices") or {}).get("switching"))

    def prepare(self, proposal_id, *, expected_context=None):
        self._enter()
        try:
            proposal = self._proposal
            state = self.controller.state()
            self._check_expected_context(state, expected_context)
            if not self._available(state):
                self._proposal = None
                raise WebError(self._unavailable_reason(), 403)
            if (not proposal or proposal_id != proposal["id"] or self.clock() - proposal["created"] > 120
                    or self._binding(state) != proposal["binding"]
                    or not self._can_prepare(state, require_scan=proposal["action"] != "pick_saved")):
                raise WebError("This assistant suggestion expired or the scene changed. Ask again before preparing a preview.", 409)
            self._proposal = None  # Never retry an uncertain submission automatically.
            payload = {"request_id": uuid.uuid4().hex}
            if proposal["action"] != "pick_saved":
                payload["capture_id"] = state["inventory"]["capture"]
            if state.get("devices"):
                payload.update(_device_id=state["devices"]["active_id"], _device_epoch=state["device_epoch"])
            if proposal["action"] == "select":
                payload.update(action="goto", cup_id=proposal["ids"][0])
                operation = "select"
            elif proposal["action"] == "save_cup":
                payload.update(cup_id=proposal["ids"][0], name=proposal["cup_name"])
                operation = "save_cup"
            elif proposal["action"] == "pick_saved":
                payload.update(id=proposal["saved_cup_id"])
                operation = "pick_saved"
            else:
                operation = "batch"
            if operation in ("select", "batch", "pick_saved"):
                return self.controller.submit(operation, payload, assistant_request_id=proposal["id"])
            return self.controller.submit(operation, payload)
        finally:
            self._lock.release()
