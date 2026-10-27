"""Single-owner web adapter. Browser requests never call the robot directly."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import secrets
import threading
import time
import uuid

from cup_inventory import SelectionError
from handover_plan import pick_position_problem


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

    def __init__(self, factory, *, mode="demo", model="qwen3:4b", llm_enabled=True):
        if mode not in ("demo", "dry_run", "live"):
            raise ValueError("Invalid mode")
        self.factory = factory
        self.session = self.language = None
        self.mode = mode
        self._lock = threading.RLock()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cup-controller")
        self._started = self._closed = False
        self._request_ids = set()
        self._images = {}
        self._proposal_deadline = None
        self._state = {
            "instance_id": uuid.uuid4().hex, "csrf_token": secrets.token_urlsafe(32),
            "mode": mode, "ready": False, "busy": False, "operation": None,
            "revision": 0, "scan_usable": False, "recovery_required": False,
            "llm_enabled": llm_enabled, "model": model, "inventory": None,
            "proposal": None, "message": "Starting local controller…", "error": None,
            "progress": None, "events": [],
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

    def start(self):
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise WebError("Controller is closed.")
            self._started = True
            self._update(busy=True, operation="startup")
            self._worker.submit(self._initialize)

    def _initialize(self):
        try:
            self.session, self.language = self.factory(self)
            if bool(self.session.robot.dry_run) != (self.mode != "live"):
                raise ValueError("Controller mode and robot mode disagree; startup refused.")
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
            self._update(busy=False, operation=None)

    def state(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            if result["proposal"] is not None:
                result["proposal"]["expires_in"] = max(0, self._proposal_deadline - time.monotonic())
            return result

    def image(self, capture_id):
        """Only serve an explicitly registered active scan image, never a path."""
        with self._lock:
            return self._images.get(capture_id)

    def publish_session(self, session=None):
        """Called by the worker after state changes, including persisted job steps."""
        session = session or self.session
        if session is None:
            return
        inventory = session.inventory
        public_inventory = None
        progress = None
        images = {}
        if inventory:
            cups = []
            for cup in inventory["cups"]:
                reason = cup.get("blocked_reason") or pick_position_problem(cup, session.tag_offset)
                visible = {key: copy.deepcopy(cup.get(key)) for key in
                           ("id", "confidence", "pixel", "bbox_xyxy", "selectable", "status")}
                visible.update(pick_available=bool(cup.get("selectable")) and reason is None,
                               blocked_reason=reason)
                cups.append(visible)
                execution = cup.get("execution")
                if execution:
                    completed = len(execution.get("completed_steps", []))
                    progress = {"step": execution.get("pending_step") or execution.get("last_completed_step"),
                                "completed": completed, "total": 23 if execution["action"] == "goto" else 2,
                                "state": execution["state"]}
            image = inventory.get("images", {}).get("annotated")
            if image:
                images[inventory["capture"]] = image
            public_inventory = {key: inventory.get(key) for key in ("capture", "saved_at", "id_scope")}
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
                        "description": ("Pick with the left arm, transfer to the right, place, and return both arms home."
                                        if record["action"] == "goto" else "Move the left arm above this cup without gripping."),
                        "expires_in": max(0, remaining)}
        with self._lock:
            previous = self._state["progress"]
            self._images = images
            self._proposal_deadline = deadline
            self._update(inventory=public_inventory, proposal=proposal,
                         scan_usable=session.scan_usable, recovery_required=session.motion_uncertain,
                         progress=progress)
            if progress and progress != previous:
                self.event(f"{progress['state'].capitalize()}: {(progress['step'] or 'sequence').replace('_', ' ')} "
                           f"({progress['completed']}/{progress['total']} steps confirmed)")

    @staticmethod
    def _validate(operation, payload):
        fields = {"scan": set(), "command": {"text"},
                  "select": {"capture_id", "cup_id", "action"},
                  "confirm": {"capture_id", "cup_id", "action", "token"},
                  "cancel": set(), "recovery": {"acknowledged"}}
        if operation not in fields or not isinstance(payload, dict) or set(payload) != fields[operation] | {"request_id"}:
            raise WebError("Invalid request fields.", 422)
        rid = payload["request_id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", rid):
            raise WebError("A valid unique request ID is required.", 422)
        if operation == "command" and (not isinstance(payload["text"], str)
                or not 1 <= len(payload["text"].strip()) <= 2000):
            raise WebError("Enter a command of 1–2000 characters.", 422)
        if operation in ("select", "confirm"):
            if (type(payload["cup_id"]) is not int or payload["cup_id"] < 1
                    or payload["action"] not in ("goto", "hover")
                    or not isinstance(payload["capture_id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", payload["capture_id"])):
                raise WebError("Choose a cup and action from the current scan.", 422)
        if operation == "confirm" and (not isinstance(payload["token"], str)
                or not re.fullmatch(r"[0-9a-f]{8}", payload["token"])):
            raise WebError("Invalid confirmation code.", 422)
        if operation == "recovery" and payload["acknowledged"] is not True:
            raise WebError("Inspect the robot and explicitly acknowledge recovery first.", 422)

    def submit(self, operation, payload):
        self._validate(operation, payload)
        with self._lock:
            if self._closed or not self._state["ready"]:
                raise WebError("Controller is not ready.", 503)
            request_id = payload["request_id"]
            if request_id in self._request_ids:
                raise WebError("This request was already received; it will not be repeated.")
            if self._state["busy"]:
                raise WebError("The controller is busy. No extra action was queued.")
            if len(self._request_ids) >= 10000:
                raise WebError("Request limit reached. Restart the app when the robot is idle.")
            self._request_ids.add(request_id)
            self._update(busy=True, operation=operation, error=None,
                         message={"scan": "Capturing and detecting cups…", "command": "Interpreting your command…",
                                  "select": "Checking the selected cup…", "confirm": "Rechecking the cup before execution…",
                                  "cancel": "Cancelling the proposal…", "recovery": "Recording your acknowledgement…"}[operation])
            self._worker.submit(self._run, operation, copy.deepcopy(payload))
        return {"accepted": True}

    def _command(self, text):
        lower = text.strip().lower()
        if lower in ("scan", "capture"):
            self.language.cancel()
            return {"kind": "inventory", "inventory": self.session.scan()}
        if lower in ("cancel", "stop"):
            self.language.cancel()
            return {"message": "Pending request cancelled. This does not stop an active robot movement."}
        if lower == "list":
            return {"kind": "inventory", "inventory": self.session.inventory}
        match = re.fullmatch(r"(goto|hover)\s+([0-9]+)", lower)
        if match:
            capture = self.session.inventory["capture"] if self.session.inventory else ""
            return self.language.propose_selection(action=match[1], capture_id=capture, cup_id=int(match[2]))
        if lower.split()[0] in ("confirm", "reset", "home", "quit", "exit", "q"):
            raise SelectionError("Use the displayed controls for confirmation or recovery. Home is not a web command.")
        if not self._state["llm_enabled"]:
            raise SelectionError("Language is disabled. Use Scan and the cup selection buttons, or restart with --llm.")
        return self.language.propose(text)

    def _run(self, operation, payload):
        try:
            result = None
            if operation == "scan":
                self.language.cancel()
                result = {"kind": "inventory", "inventory": self.session.scan()}
            elif operation == "command":
                result = self._command(payload["text"])
            elif operation == "select":
                self.language.cancel()
                result = self.language.propose_selection(**{k: payload[k] for k in ("action", "capture_id", "cup_id")})
            elif operation == "confirm":
                pending = self.language.pending
                if not pending or any(payload[key] != pending["record"][key]
                                      for key in ("capture_id", "cup_id", "action")):
                    self.language.cancel()
                    raise SelectionError("This confirmation no longer matches the displayed selection. Select again.")
                self._update(proposal=None)
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
                result = {"message": "Inspection acknowledged. No robot command sent. Scan again."}
            message = (result or {}).get("message")
            if (result or {}).get("kind") == "proposal":
                record = self.language.pending["record"]
                action = "pick, handover, place and return home" if record["action"] == "goto" else "hover only"
                message = f"Cup {record['cup_id']} selected for {action}. Review the highlighted cup and use the confirmation button."
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
                           if result["action"] == "goto" else "Robot reported hover complete. Scan again.")
            self._update(message=message or "Done.")
            self.event(message or "Done.")
        except BaseException as error:
            # Keep all motion recovery decisions inside the existing CupSession.
            # Cancel any remaining interpretation so an error cannot resurrect it.
            try:
                self.language.cancel()
            except BaseException:
                pass
            self._update(error=str(error), message=f"Blocked: {error}")
            self.event(f"Blocked: {error}", "error")
        finally:
            try:
                self.publish_session()
            finally:
                self._update(busy=False, operation=None)

    def close(self):
        with self._lock:
            self._closed = True
            self._update(ready=False)
        # Do not pretend closing a UI/server is a physical emergency stop.
        self._worker.shutdown(wait=True, cancel_futures=False)
