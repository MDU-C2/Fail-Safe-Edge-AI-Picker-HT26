"""Single-owner web adapter. Browser requests never call the robot directly."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import secrets
import threading
import time
import uuid

from cup_inventory import SelectionError, hover_moves
from handover_plan import (FLIP_ACCELERATION, FLIP_SPEED, handover_jobs,
                           normalize_handover_offset, pick_position_problem)
from operator_commands import COMMAND_HELP, START_COMMANDS, STOP_COMMANDS
from robot_controls import RobotControls, control_plan, valid_speed


class WebError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class WebController:
    """One worker owns all session/model/camera mutations; readers see copies.

    There is no movement queue, request retry, or direct robot route. Request IDs
    live for this server instance; a restart changes its CSRF capability and has
    no scan or proposal. The existing persistent robot recovery journal survives.
    """

    def __init__(self, factory, *, mode="demo", model="qwen3-vl:4b-instruct", llm_enabled=True):
        if mode not in ("demo", "dry_run", "live"):
            raise ValueError("Invalid mode")
        self.factory = factory
        self.session = self.language = None
        self.controls = None
        self.mode = mode
        self._lock = threading.RLock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cup-controller")
        self._stop_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cup-stop")
        self._normal_busy = self._stop_busy = False
        self._normal_operation = None
        self._generation = 0
        self._started = self._closed = False
        self._request_ids = set()
        self._images = {}
        self._proposal_deadline = None
        self._control_deadline = None
        self._execution_capture = None
        self._execution_totals = {}
        self._state = {
            "instance_id": uuid.uuid4().hex, "csrf_token": secrets.token_urlsafe(32),
            "mode": mode, "ready": False, "busy": False, "operation": None,
            "revision": 0, "scan_usable": False, "recovery_required": False,
            "llm_enabled": llm_enabled, "model": model, "inventory": None,
            "proposal": None, "message": "Starting local controller…", "error": None,
            "progress": None, "batch": None, "motion": None, "placement_grid": None, "events": [],
            "stop_requested": False, "stop_in_flight": False, "stop": None,
            "robot_controls": {"status": None, "proposal": None, "home_speed": None},
            "saved_cups": {"enabled": False, "matcher_enabled": False, "cups": []},
        }
        self._event_id = 0

    def _update(self, **fields):
        with self._lock:
            self._state.update(copy.deepcopy(fields))
            self._state["revision"] += 1

    def event(self, message, level="info"):
        with self._lock:
            self._event_id += 1
            self._state["events"].append({"id": self._event_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "level": level, "message": str(message)[:2000]})
            self._state["events"] = self._state["events"][-100:]
            self._state["revision"] += 1

    def _publish_busy(self):
        """Call with the controller lock held; both workers must finish before idle."""
        self._update(busy=self._normal_busy or self._stop_busy,
                     operation=self._normal_operation if self._normal_busy else "stop" if self._stop_busy else None,
                     stop_in_flight=self._stop_busy)

    def _check_generation(self, generation):
        with self._lock:
            if generation != self._generation:
                raise SelectionError("This request was cancelled by Stop. Inspect and acknowledge recovery before continuing.")

    def status(self):
        """Only copied application state: never wait for a camera/model/robot call."""
        state = self.state()
        keys = ("mode", "ready", "busy", "operation", "scan_usable", "recovery_required",
                "stop_requested", "stop_in_flight", "stop", "progress", "batch", "motion", "message", "error")
        result = {key: state[key] for key in keys}
        result["capture_id"] = (state.get("inventory") or {}).get("capture")
        result["source"] = "Cached application state; not a live robot status query."
        return result

    @staticmethod
    def help():
        return {"message": COMMAND_HELP}

    def start(self):
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise WebError("Controller is closed.")
            self._started = True
            self._normal_busy, self._normal_operation = True, "startup"
            self._publish_busy()
            self._worker.submit(self._initialize)

    def _initialize(self):
        try:
            self.session, self.language = self.factory(self)
            if bool(self.session.robot.dry_run) != (self.mode != "live"):
                raise ValueError("Controller mode and robot mode disagree; startup refused.")
            self.controls = RobotControls(self.session)
            self.publish_session()
            message = ("Demo ready. Sample cups and scripted language; no hardware or model connection."
                       if self.mode == "demo" else "Ready. Scan the table to begin.")
            self._update(ready=True, message=message)
            self.event(message)
        except BaseException as error:
            # Startup helpers historically use SystemExit for missing weights/token.
            self._update(ready=False, error=str(error), message="Startup failed. Check the terminal and restart.")
            self.event(f"Startup failed: {error}", "error")
        finally:
            with self._lock:
                self._normal_busy, self._normal_operation = False, None
                self._publish_busy()

    def state(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            if result["proposal"] is not None:
                result["proposal"]["expires_in"] = max(0, self._proposal_deadline - time.monotonic())
            if result["robot_controls"]["proposal"] is not None:
                result["robot_controls"]["proposal"]["expires_in"] = max(0, self._control_deadline - time.monotonic())
            return result

    def image(self, capture_id):
        """Only serve an explicitly registered active scan image, never a path."""
        with self._lock:
            return self._images.get(capture_id)

    def saved_image(self, cup_id):
        if not isinstance(cup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", cup_id):
            return None
        try:
            if self.language is not None and self.language.named_cups is not None:
                return self.language.named_cups.get_id(cup_id)["image_bytes"]
        except (OSError, ValueError):
            pass
        return None

    def publish_session(self, session=None):
        """Called by the worker after state changes, including persisted job steps."""
        session = session or self.session
        if session is None:
            return
        inventory = session.inventory
        public_inventory = None
        progress = None
        public_batch = None
        images = {}
        saved_cups = {"enabled": False, "matcher_enabled": False, "cups": []}
        if self.language is not None:
            try:
                saved_cups = self.language.saved_cups()
                for saved in saved_cups["cups"]:
                    saved["thumbnail_url"] = f"/api/saved-image/{saved['id']}"
            except (OSError, ValueError) as error:
                saved_cups["error"] = str(error)
        motion = {"speed": session.speed, "flip_speed": min(session.speed, FLIP_SPEED),
                  "acceleration": 100, "flip_acceleration": FLIP_ACCELERATION,
                  "handover": session.motion_settings()["handover"]}
        grid = None
        if session.placing_grid is not None:
            try:
                grid = {"locations": [{key: copy.deepcopy(place[key]) for key in
                        ("id", "x", "y", "z", "occupied", "reserved", "vision_blocked") if key in place}
                        for place in session.placing_grid.snapshot()["locations"]]}
            except ValueError as error:
                # A bad grid blocks picking through the session; hover and the
                # status screen remain available so the operator can inspect it.
                grid = {"locations": [], "error": str(error)}
        if inventory:
            cups = []
            for cup in inventory["cups"]:
                reason = cup.get("blocked_reason") or pick_position_problem(cup, session.tag_offset)
                visible = {key: copy.deepcopy(cup.get(key)) for key in
                           ("id", "confidence", "pixel", "bbox_xyxy", "selectable", "status")}
                visible.update(pick_available=bool(cup.get("selectable")) and reason is None,
                               blocked_reason=reason,
                               can_save=(cup.get("cup_detection_id") is not None
                                         and bool(inventory.get("images", {}).get("rgb"))))
                cups.append(visible)
                execution = cup.get("execution")
                if execution:
                    completed = len(execution.get("completed_steps", []))
                    progress = {"step": execution.get("pending_step") or execution.get("last_completed_step"),
                                "completed": completed, "total": execution.get("total_steps", completed),
                                "state": execution["state"]}
            batch = session.batch_progress
            if batch:
                ids = batch["cup_ids"]
                current_id = batch.get("current_cup_id")
                completed_cups = batch.get("completed_cups", [])
                public_batch = {"current": ids.index(current_id) + 1 if current_id in ids else 0,
                                "total": len(ids), "cup_id": current_id,
                                "completed_cups": list(completed_cups), "state": batch["state"]}
                by_id = {cup["id"]: cup for cup in inventory["cups"]}
                total = completed = 0
                for cup_id in ids:
                    cup = by_id[cup_id]
                    execution = cup.get("execution", {})
                    verified_jobs = cup.get("last_verification", {}).get("jobs", [])
                    planned = (self._execution_totals.get(cup_id, 0)
                               if self._execution_capture == inventory["capture"] else 0)
                    total += execution.get("total_steps", planned or len(verified_jobs))
                    completed += (len(verified_jobs) if session.robot.dry_run and cup_id in completed_cups
                                  else len(execution.get("completed_steps", [])))
                current_execution = by_id.get(current_id, {}).get("execution", {})
                step = current_execution.get("pending_step") or current_execution.get("last_completed_step")
                progress = {"step": step or ("preview_complete" if batch["state"] == "previewed" else "checking_cup"),
                            "completed": completed, "total": total, "state": batch["state"],
                            "batch": public_batch}
            image = inventory.get("images", {}).get("annotated")
            if image:
                images[inventory["capture"]] = image
            public_inventory = {key: inventory.get(key) for key in ("capture", "saved_at", "id_scope")}
            if "placement_observation" in inventory:
                observation = inventory["placement_observation"]
                public_inventory["placement_observation"] = {key: copy.deepcopy(observation[key]) for key in
                    ("frame_count", "applied", "message", "released_ids", "slots") if key in observation}
            public_inventory.update(cups=cups, image_url=(f"/api/image/{inventory['capture']}" if image else None))
        proposal = None
        deadline = None
        if self.language is not None and self.language.pending:
            pending = self.language.pending
            record = pending["record"]
            remaining = self.language.proposal_ttl - (self.language.clock() - pending["created"])
            deadline = time.monotonic() + max(0, remaining)
            proposal = {"token": pending["token"], "capture_id": record["capture_id"],
                        "cup_id": record["cup_id"], "action": record["action"],
                        "cup_ids": list(record.get("cup_ids", [record["cup_id"]])),
                        "placements": [{key: copy.deepcopy(place[key]) for key in ("id", "x", "y", "z")}
                                       for place in (record.get("placement") or {}).get("locations", [])],
                        "excluded_cup_ids": list(record.get("excluded_cup_ids", [])),
                        "speed": record.get("speed", session.speed),
                        "flip_speed": record.get("flip_speed", min(session.speed, FLIP_SPEED)),
                        "handover": copy.deepcopy(record.get("handover")),
                        "description": record.get("description") or (
                            "Pick with the left arm, transfer to the right, place, and return both arms home."
                            if record["action"] in ("goto", "goto_all") else
                            "Move the left arm above this cup without gripping."),
                        "expires_in": max(0, remaining)}
            if "saved_cup" in record:
                proposal["saved_cup"] = copy.deepcopy(record["saved_cup"])
                proposal["saved_cup"]["thumbnail_url"] = f"/api/saved-image/{record['saved_cup']['id']}"
        with self._lock:
            previous = self._state["progress"]
            stopped = self._state["stop_requested"] or session.stop_requested
            controls = self.controls.snapshot() if self.controls else self._state["robot_controls"]
            if stopped:
                # A model/camera callback can finish after Stop. Its old result
                # is never exposed as a fresh proposal or authorization to move.
                proposal = None
                deadline = None
                controls["proposal"] = None
            self._control_deadline = (time.monotonic() + controls["proposal"]["expires_in"]
                                      if controls["proposal"] else None)
            self._images = images
            self._proposal_deadline = deadline
            self._update(inventory=public_inventory, proposal=proposal,
                         scan_usable=session.scan_usable and not stopped,
                         recovery_required=session.motion_uncertain or stopped,
                         stop_requested=stopped,
                         progress=progress, batch=public_batch, motion=motion, placement_grid=grid,
                         robot_controls=controls, saved_cups=saved_cups)
            if progress and progress != previous:
                self.event(f"{progress['state'].capitalize()}: {(progress['step'] or 'sequence').replace('_', ' ')} "
                           f"({progress['completed']}/{progress['total']} steps "
                           f"{'confirmed' if self.mode == 'live' else 'previewed'})")

    def _prepare_progress(self, record):
        """Count the bound pure plans before dispatch, including not-yet-started cups."""
        inventory = self.session.inventory
        if inventory is None or inventory["capture"] != record["capture_id"]:
            raise SelectionError("The scan changed. Select the cup again.")
        cups = {cup["id"]: cup for cup in inventory["cups"]}
        ids = record.get("cup_ids", [record["cup_id"]])
        if record["action"] == "hover":
            totals = {record["cup_id"]: len(hover_moves(cups[record["cup_id"]],
                      tag_offset=self.session.tag_offset, speed=self.session.speed))}
        else:
            places = (record.get("placement") or {}).get("locations", [])
            if len(places) != len(ids):
                raise SelectionError("The proposal has no matching placement destinations. Select again.")
            totals = {cup_id: len(handover_jobs(cups[cup_id], place=place,
                      tag_offset=self.session.tag_offset, speed=self.session.speed,
                      handover_offset=self.session.handover_offset))
                      for cup_id, place in zip(ids, places)}
        self._execution_capture = record["capture_id"]
        self._execution_totals = totals

    @staticmethod
    def _validate(operation, payload):
        fields = {"scan": set(), "start": set(), "stop": set(), "status": set(), "help": set(), "command": {"text"},
                  "select": {"capture_id", "cup_id", "action"}, "batch": {"capture_id"},
                  "confirm": {"capture_id", "cup_id", "action", "token"},
                  "cancel": set(), "recovery": {"acknowledged"}, "robot_status": set(), "speed": {"speed"},
                  "control_preview": {"action", "arm"}, "control_confirm": {"token", "acknowledged"},
                  "control_cancel": set(), "handover_offset": {"x", "y", "z"},
                  "save_cup": {"capture_id", "cup_id", "name"},
                  "pick_saved": {"id"}, "forget_cup": {"id"}}
        if operation not in fields or not isinstance(payload, dict) or set(payload) != fields[operation] | {"request_id"}:
            raise WebError("Invalid request fields.", 422)
        rid = payload["request_id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", rid):
            raise WebError("A valid unique request ID is required.", 422)
        if operation == "command" and (not isinstance(payload["text"], str)
                or not 1 <= len(payload["text"].strip()) <= 2000):
            raise WebError("Enter a command of 1–2000 characters.", 422)
        if operation in ("select", "batch", "confirm", "save_cup"):
            if (not isinstance(payload["capture_id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", payload["capture_id"])):
                raise WebError("Choose a cup and action from the current scan.", 422)
        if operation == "save_cup":
            if (type(payload["cup_id"]) is not int or payload["cup_id"] < 1
                    or not isinstance(payload["name"], str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,47}", payload["name"].strip())):
                raise WebError("Choose a cup and a name of 1–48 letters, digits, spaces, underscores or hyphens, starting with a letter.", 422)
        if operation in ("pick_saved", "forget_cup") and (not isinstance(payload["id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", payload["id"])):
            raise WebError("Choose a saved cup from this library.", 422)
        if operation in ("select", "confirm"):
            batch = operation == "confirm" and payload["action"] == "goto_all"
            valid = (payload["cup_id"] is None if batch else
                     type(payload["cup_id"]) is int and payload["cup_id"] > 0
                     and payload["action"] in ("goto", "hover"))
            if not valid:
                raise WebError("Choose a cup and action from the current scan.", 422)
        if operation == "confirm" and (not isinstance(payload["token"], str)
                or not re.fullmatch(r"[0-9a-f]{8}", payload["token"])):
            raise WebError("Invalid confirmation code.", 422)
        if operation == "recovery" and payload["acknowledged"] is not True:
            raise WebError("Inspect the robot and explicitly acknowledge recovery first.", 422)
        if operation in ("speed", "control_preview"):
            try:
                if operation == "speed":
                    valid_speed(payload["speed"])
                else:
                    control_plan(payload["action"], payload["arm"], 50)
            except (ValueError, TypeError, OverflowError) as error:
                raise WebError(str(error), 422) from error
        if operation == "handover_offset":
            try:
                normalize_handover_offset(tuple(payload[a] for a in "xyz"))
            except (ValueError, TypeError, OverflowError) as error:
                raise WebError(str(error), 422) from error
        if operation == "control_confirm" and (payload["acknowledged"] is not True
                or not isinstance(payload["token"], str)
                or not re.fullmatch(r"[0-9a-f]{16}", payload["token"])):
            raise WebError("Review the action and explicitly acknowledge inspection before confirming.", 422)

    def submit(self, operation, payload):
        self._validate(operation, payload)
        if operation == "command":
            direct = re.sub(r"\s+", " ", payload["text"].strip().lower())
            if direct in STOP_COMMANDS:
                operation = "stop"
            elif direct in ("status", "help"):
                operation = direct
        with self._lock:
            if self._closed or not self._state["ready"]:
                raise WebError("Controller is not ready.", 503)
            request_id = payload["request_id"]
            if request_id in self._request_ids:
                raise WebError("This request was already received; it will not be repeated.")
            if operation in ("status", "help"):
                # Read-only quick commands remain available while either worker
                # is running and never change a pending selection or its expiry.
                return {"accepted": True, operation: self.status() if operation == "status" else self.help()}
            if operation == "stop":
                if self._stop_busy:
                    raise WebError("A software stop request is already in progress.")
                self._request_ids.add(request_id)
                self._generation += 1
                self._stop_busy = True
                self.session.latch_stop()  # Must remain instant and free of network/disk I/O.
                message = "Software stop requested. Further commands are blocked; waiting for the controller response."
                self._update(proposal=None, scan_usable=False, recovery_required=True, stop_requested=True,
                             stop={"state": "requested", "stopped": None, "dry_run": self.mode != "live", "message": message},
                             message=message)
                self._proposal_deadline = None
                self._control_deadline = None
                self._state["robot_controls"]["proposal"] = None
                self._publish_busy()
                self.event(message, "warning")
                self._stop_worker.submit(self._run_stop)
                return {"accepted": True}
            if self._state["busy"]:
                raise WebError("The controller is busy. No extra action was queued.")
            if self._state["stop_requested"] and operation not in ("recovery", "cancel", "robot_status", "control_cancel"):
                raise WebError("Software stop is latched. Inspect the robot and acknowledge recovery before a new action.")
            if len(self._request_ids) >= 10000:
                raise WebError("Request limit reached. Restart the app when the robot is idle.")
            self._request_ids.add(request_id)
            self._normal_busy, self._normal_operation = True, operation
            self._publish_busy()
            self._update(error=None,
                         message={"scan": "Capturing and detecting cups…", "command": "Interpreting your command…",
                                  "start": "Scanning now to prepare a pick-all preview…",
                                  "select": "Checking the selected cup…", "confirm": "Rechecking the cup before execution…",
                                  "batch": "Checking cups and available placement locations…",
                                  "cancel": "Cancelling the proposal…", "recovery": "Recording your acknowledgement…",
                                  "robot_status": "Reading controller status…", "speed": "Updating future movement speed…",
                                  "handover_offset": "Updating future handover alignment…",
                                  "save_cup": "Saving a local cup reference…",
                                  "pick_saved": "Scanning the table and comparing the saved cup with the local vision model…",
                                  "forget_cup": "Removing the saved reference…",
                                  "control_preview": "Preparing robot control preview…",
                                  "control_confirm": "Executing the reviewed robot control action…",
                                  "control_cancel": "Cancelling robot control preview…"}[operation])
            self._worker.submit(self._run, operation, copy.deepcopy(payload), self._generation)
        return {"accepted": True}

    def _run_stop(self):
        """Independent of the movement/model worker and its language lock."""
        try:
            result = self.session.request_stop()
            dry_run = result.get("dry_run") is True
            stopped = result.get("stopped") is True
            state = "previewed" if dry_run else "confirmed" if stopped else "unconfirmed"
            message = result.get("message") or (
                "Software stop previewed; no robot request sent. New commands remain blocked." if dry_run else
                "The controller confirmed its software stop. Inspect the robot before recovery." if stopped else
                "Software stop is unconfirmed. Use the robot's physical emergency stop if needed.")
            with self._lock:
                fields = {"stop": {"state": state, "dry_run": dry_run, "stopped": stopped, "message": message},
                          "message": message, "proposal": None, "scan_usable": False,
                          "recovery_required": True, "stop_requested": True}
                if state == "unconfirmed":
                    fields["error"] = message
                self._update(**fields)
                self.event(message, "warning" if state != "unconfirmed" else "error")
        except BaseException as error:
            message = f"Software stop is unconfirmed: {error}. Use the robot's physical emergency stop if needed."
            self._update(stop={"state": "unconfirmed", "dry_run": self.mode != "live", "stopped": False, "message": message},
                         error=message, message=message, proposal=None, scan_usable=False,
                         recovery_required=True, stop_requested=True)
            self.event(message, "error")
        finally:
            with self._lock:
                self._stop_busy = False
                self._publish_busy()

    def _command(self, text):
        lower = text.strip().lower()
        if lower in ("scan", "capture"):
            self.language.cancel()
            return {"kind": "inventory", "inventory": self.session.scan()}
        if re.sub(r"\s+", " ", lower) in START_COMMANDS:
            return self.language.start()
        if lower == "cancel":
            self.language.cancel()
            return {"message": "Pending request cancelled. This does not stop an active robot movement."}
        if lower == "list":
            return {"kind": "inventory", "inventory": self.session.inventory}
        if re.sub(r"\s+", " ", lower) in ("goto all", "pick all", "pick all cups", "pick all available cups"):
            capture = self.session.inventory["capture"] if self.session.inventory else ""
            return self.language.propose_batch(capture_id=capture)
        match = re.fullmatch(r"(goto|hover)\s+([0-9]+)", lower)
        if match:
            capture = self.session.inventory["capture"] if self.session.inventory else ""
            return self.language.propose_selection(action=match[1], capture_id=capture, cup_id=int(match[2]))
        if lower.split()[0] in ("confirm", "reset", "home", "quit", "exit", "q"):
            raise SelectionError("Use the displayed controls for confirmation or recovery. Home is not a web command; use Robot controls.")
        named = self.language.named_command(text)
        if named is not None:
            return named
        if not self._state["llm_enabled"]:
            raise SelectionError("Language is disabled. Use Scan and the cup selection buttons, or restart with --llm.")
        return self.language.propose(text)

    def _run(self, operation, payload, generation):
        try:
            self._check_generation(generation)
            result = None
            if operation not in ("control_confirm", "robot_status"):
                self.controls.cancel()
            if operation in ("speed", "handover_offset", "control_preview", "control_confirm"):
                self.language.cancel()
            if operation == "start":
                result = self.language.start()
            elif operation == "scan":
                self.language.cancel()
                result = {"kind": "inventory", "inventory": self.session.scan()}
            elif operation == "command":
                result = self._command(payload["text"])
            elif operation == "select":
                self.language.cancel()
                result = self.language.propose_selection(**{k: payload[k] for k in ("action", "capture_id", "cup_id")})
            elif operation == "batch":
                self.language.cancel()
                result = self.language.propose_batch(capture_id=payload["capture_id"])
            elif operation == "save_cup":
                result = self.language.save_cup(**{key: payload[key] for key in ("capture_id", "cup_id", "name")})
            elif operation == "pick_saved":
                result = self.language.propose_saved(payload["id"])
            elif operation == "forget_cup":
                result = self.language.forget_cup(payload["id"])
            elif operation == "confirm":
                pending = self.language.pending
                if not pending or any(payload[key] != pending["record"][key]
                                      for key in ("capture_id", "cup_id", "action")):
                    self.language.cancel()
                    raise SelectionError("This confirmation no longer matches the displayed selection. Select again.")
                self._prepare_progress(pending["record"])
                self._update(proposal=None)
                self._check_generation(generation)
                result = self.language.confirm(payload["token"])
                if self.mode != "live":
                    self.event("Planned steps only: " + " → ".join(job["name"].replace("_", " ")
                                                                  for job in result.get("jobs", [])))
            elif operation == "cancel":
                self.language.cancel()
                result = {"message": "Proposal cancelled. No movement requested."}
            elif operation == "recovery":
                self.language.cancel()
                self.session.acknowledge_recovery()
                with self._lock:
                    self._check_generation(generation)
                    self._update(stop_requested=False, stop=None)
                result = {"message": "Inspection acknowledged. No robot command sent. Scan again."}
            elif operation == "robot_status":
                result = self.controls.refresh_status()
            elif operation == "speed":
                result = self.controls.set_speed(payload["speed"])
            elif operation == "handover_offset":
                result = self.controls.set_handover_offset(tuple(payload[a] for a in "xyz"))
            elif operation == "control_preview":
                result = self.controls.propose(payload["action"], payload["arm"])
            elif operation == "control_confirm":
                result = self.controls.confirm(payload["token"])
            elif operation == "control_cancel":
                result = {"message": "Robot control preview cancelled. No command sent."}
            self._check_generation(generation)
            message = (result or {}).get("message")
            if (result or {}).get("kind") == "proposal":
                record = self.language.pending["record"]
                action = "pick, handover, place and return home" if record["action"] in ("goto", "goto_all") else "hover only"
                target = ("Cups " + ", ".join(str(value) for value in record["cup_ids"])
                          if record["action"] == "goto_all" else f"Cup {record['cup_id']}")
                if "saved_cup" in record:
                    target += f" matched to saved reference {record['saved_cup']['name']}"
                message = f"{target} selected for {action}. Review the highlighted selection and destinations, then confirm."
            elif (result or {}).get("kind") == "inventory":
                inventory = result.get("inventory")
                count = len(inventory["cups"]) if inventory else 0
                message = f"{count} cup candidate(s) in the current scan. Select a cup to preview an action."
            elif (result or {}).get("kind") == "cup":
                cup = result["cup"]
                message = f"Cup {cup['id']}: {cup.get('blocked_reason') or cup['status']}."
            elif (result or {}).get("kind") in ("reply", "clarify"):
                message = "LLM response — no robot action requested: " + message
            if operation == "confirm" and (result or {}).get("kind") == "result" and self.mode == "live":
                message = ("Robot reported the sequence complete. Placement is not independently verified. Scan again."
                           if result["action"] in ("goto", "goto_all") else "Robot reported hover complete. Scan again.")
            with self._lock:
                self._check_generation(generation)
                self._update(message=message or "Done.")
                self.event(message or "Done.")
        except BaseException as error:
            # Keep all motion recovery decisions inside the existing CupSession.
            # Cancel any remaining interpretation so an error cannot resurrect it.
            try:
                self.language.cancel()
                self.controls.cancel()
            except BaseException:
                pass
            with self._lock:
                if generation == self._generation:
                    self._update(error=str(error), message=f"Blocked: {error}")
                self.event(f"Blocked: {error}", "error")
        finally:
            try:
                if generation != self._generation:
                    self.session.scan_usable = False
                    try:
                        self.language.cancel()
                        self.controls.cancel()
                    except BaseException:
                        pass
                self.publish_session()
            finally:
                with self._lock:
                    self._normal_busy, self._normal_operation = False, None
                    self._publish_busy()

    def close(self):
        with self._lock:
            self._closed = True
            self._update(ready=False)
        # Do not pretend closing a UI/server is a physical emergency stop.
        self._worker.shutdown(wait=True, cancel_futures=False)
        self._stop_worker.shutdown(wait=True, cancel_futures=False)
