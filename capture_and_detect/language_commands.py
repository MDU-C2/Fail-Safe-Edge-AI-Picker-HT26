"""Local-language proposals; only explicit operator confirmation requests motion."""

import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from cup_inventory import SelectionError, atomic_json, finite_number, selected_cup
from handover_plan import pick_position_problem
from local_llm import validate_intent


def scene_summary(session):
    """Send only selection facts to the model, never credentials or robot commands."""
    inventory = session.inventory
    cups = []
    for cup in inventory["cups"] if inventory else []:
        pick_reason = cup.get("blocked_reason") or pick_position_problem(cup, session.tag_offset)
        cups.append({"id": cup["id"], "image_x": cup["pixel"].get("u"),
                     "image_y": cup["pixel"].get("v"),
                     "hover_available": bool(cup.get("selectable")),
                     "pick_available": bool(cup.get("selectable")) and pick_reason is None,
                     "blocked_reason": pick_reason, "status": cup["status"]})
    return {"capture_id": inventory["capture"] if inventory else None,
            "scan_usable": session.scan_usable, "recovery_required": session.motion_uncertain,
            "direction_reference": "left/right in the numbered camera image",
            "known_attributes": ["id", "image_position", "availability"], "cups": cups}


def inventory_fingerprint(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, allow_nan=False).encode()).hexdigest()


class LanguageController:
    """One request, one grounded proposal, one explicit confirmation, no retries."""

    def __init__(self, session, interpreter, *, audit_dir=None, proposal_ttl=60,
                 clock=time.monotonic):
        if not finite_number(proposal_ttl) or proposal_ttl <= 0:
            raise ValueError("proposal_ttl must be a positive finite number")
        self.session = session
        self.interpreter = interpreter
        self.audit_dir = Path(audit_dir) if audit_dir is not None else None
        self.proposal_ttl = proposal_ttl
        self.clock = clock
        self.pending = None
        self._lock = threading.Lock()

    def _save(self, record):
        if self.audit_dir is not None:
            atomic_json(self.audit_dir / f"language_{record['request_id']}.json", record)

    def _cancel(self):
        pending, self.pending = self.pending, None
        if pending is not None:
            record = pending["record"]
            record["state"] = "cancelled"
            self._save(record)

    def cancel(self):
        if not self._lock.acquire(blocking=False):
            raise SelectionError("A language request is running. This is not a robot stop command.")
        try:
            self._cancel()
        finally:
            self._lock.release()

    def _target(self, intent):
        inventory = self.session.inventory
        if inventory is None:
            raise SelectionError("Use scan before selecting a cup.")
        cups = inventory["cups"]
        if intent["selector"] == "id":
            candidates = [cup for cup in cups if cup["id"] == intent["cup_id"]]
        else:
            # Include blocked cups: 'leftmost' must never silently mean the next
            # reachable cup. Equal pixel coordinates require an explicit ID.
            if not cups or any(not finite_number(cup.get("pixel", {}).get("u")) for cup in cups):
                raise SelectionError("Cannot resolve left/right reliably. Use a numbered cup ID.")
            edge = (min if intent["selector"] == "leftmost" else max)(cup["pixel"]["u"] for cup in cups)
            candidates = [cup for cup in cups if abs(cup["pixel"]["u"] - edge) < 1]
        if len(candidates) != 1:
            raise SelectionError("The cup reference is missing or ambiguous. Use a numbered cup ID.")
        return candidates[0]

    def propose_selection(self, *, action, capture_id, cup_id):
        """An operator's explicit UI selection uses the same one-use proposal."""
        return self._propose(f"Operator selected {action} cup {cup_id}", {
            "action": action, "selector": "id", "cup_id": cup_id,
            "message": "Explicit operator selection.",
        }, capture_id=capture_id)

    def propose(self, text):
        return self._propose(text)

    def _propose(self, text, operator_intent=None, capture_id=None):
        if not self._lock.acquire(blocking=False):
            raise SelectionError("A language request is already running; no request was queued.")
        record = {"request_id": uuid.uuid4().hex, "text": text, "state": "interpreting"}
        try:
            self._cancel()
            if operator_intent is not None:
                if operator_intent["action"] not in ("goto", "hover"):
                    raise SelectionError("Choose pick and transfer or hover.")
                selected_cup(self.session.inventory, capture_id, operator_intent["cup_id"])
            scene = scene_summary(self.session)
            record["capture_id"] = scene["capture_id"]
            fingerprint = inventory_fingerprint(self.session.inventory)
            intent = validate_intent(operator_intent if operator_intent is not None else
                                     self.interpreter.interpret(text, scene))
            record["intent"] = intent
            if fingerprint != inventory_fingerprint(self.session.inventory):
                raise SelectionError("The scan changed while interpreting. Repeat your request using the current scan.")
            action = intent["action"]
            if action in ("clarify", "reply"):
                record["state"] = action
                self._save(record)
                return {"kind": action, "message": intent["message"]}
            if action in ("scan", "list"):
                record["state"] = "read_requested"
                self._save(record)
                result = self.session.scan() if action == "scan" else self.session.inventory
                record["state"] = "completed"
                self._save(record)
                return {"kind": "inventory", "inventory": result}
            target = self._target(intent)
            if action == "show":
                record["state"] = "completed"
                self._save(record)
                return {"kind": "cup", "cup": target}
            if self.session.motion_uncertain:
                raise SelectionError("Recovery is required. Check the robot; only the operator can use reset.")
            if not self.session.scan_usable:
                raise SelectionError("Run scan before requesting a new movement.")
            selected_cup(self.session.inventory, scene["capture_id"], target["id"])
            if action == "goto":
                reason = pick_position_problem(target, self.session.tag_offset)
                if reason:
                    raise SelectionError(reason)
            record.update(state="proposed", action=action, cup_id=target["id"])
            token = record["request_id"][:8]
            self._save(record)  # A proposal is unusable unless its audit was saved.
            self.pending = {"token": token, "created": self.clock(), "record": record,
                            "fingerprint": fingerprint}
            description = ("pick with the left arm, hand to the right, place, and return both arms home"
                           if action == "goto" else "move the left arm above it without gripping")
            mode = "Preview only" if self.session.robot.dry_run else "Physical movement"
            return {"kind": "proposal", "token": token,
                    "message": f"{mode}: cup {target['id']} — {description}.\n"
                               f"Scan: {scene['capture_id']}. Check its numbered image.\n"
                               f"Type confirm {token} within {self.proposal_ttl:g}s, or cancel."}
        except BaseException as error:
            record.update(state="blocked", error=str(error))
            try:
                self._save(record)
            except OSError:
                pass
            raise
        finally:
            self._lock.release()

    def confirm(self, token):
        if not self._lock.acquire(blocking=False):
            raise SelectionError("A language request is already running; no movement was queued.")
        try:
            # Consume before validation or execution: repetition cannot replay a
            # failed or successful physical action.
            pending, self.pending = self.pending, None
            if pending is None:
                raise SelectionError("No pending language proposal. Make a new request first.")
            record = pending["record"]
            try:
                age = self.clock() - pending["created"]
                if token != pending["token"]:
                    raise SelectionError("Confirmation does not match the displayed proposal. Request it again.")
                if not math.isfinite(age) or age < 0 or age > self.proposal_ttl:
                    raise SelectionError("The proposal expired. Request it again using the current scan.")
                if pending["fingerprint"] != inventory_fingerprint(self.session.inventory):
                    raise SelectionError("The scan or cup status changed. Make a new request.")
                record["state"] = "confirmed"
                self._save(record)
                jobs = self.session.execute_selection(action=record["action"],
                    capture_id=record["capture_id"], cup_id=record["cup_id"])
                record["state"] = "previewed" if self.session.robot.dry_run else "completed"
                self._save(record)
                return {"kind": "result", "action": record["action"], "jobs": jobs,
                        "message": "Preview complete; no robot commands sent." if self.session.robot.dry_run else
                        ("Robot reported pick, handover and placement complete; home poses checked. Scan again."
                         if record["action"] == "goto" else "Robot reported hover complete. Scan again.")}
            except BaseException as error:
                record.update(state="blocked_or_interrupted", error=str(error))
                try:
                    self._save(record)
                except OSError:
                    pass
                raise
        finally:
            self._lock.release()
