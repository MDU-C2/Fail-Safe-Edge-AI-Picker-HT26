"""Local-language proposals; only explicit operator confirmation requests motion."""

import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from cup_inventory import SelectionError, atomic_json, finite_number, selected_cup
from handover_plan import FLIP_SPEED, pick_position_problem
from local_llm import validate_intent
from operator_commands import COMMAND_HELP


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
                 clock=time.monotonic, named_cups=None, cup_matcher=None):
        if not finite_number(proposal_ttl) or proposal_ttl <= 0:
            raise ValueError("proposal_ttl must be a positive finite number")
        self.session = session
        self.interpreter = interpreter
        self.audit_dir = Path(audit_dir) if audit_dir is not None else None
        self.proposal_ttl = proposal_ttl
        self.clock = clock
        self.named_cups = named_cups
        self.cup_matcher = cup_matcher
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
        named = self.named_command(text)
        if named is not None:
            return named
        return self._propose(text)

    def saved_cups(self):
        return {"enabled": self.named_cups is not None,
                "matcher_enabled": self.cup_matcher is not None,
                "cups": self.named_cups.list() if self.named_cups is not None else []}

    def _require_library(self):
        if self.named_cups is None:
            raise SelectionError("Saved cups are unavailable in this session. Use camera mode to save a cup.")
        return self.named_cups

    def _edit_saved(self, edit):
        if not self._lock.acquire(blocking=False):
            raise SelectionError("A language request is running; wait before editing saved cups.")
        try:
            self._cancel()
            self._require_library()
            if not self.session._activity_lock.acquire(blocking=False):
                raise SelectionError("An operation is running; wait before editing saved cups.")
            try:
                self.session._check_stop()
                if self.session.motion_uncertain:
                    raise SelectionError("Inspect the robot and acknowledge recovery before editing saved cups.")
                return edit()
            finally:
                self.session._activity_lock.release()
        finally:
            self._lock.release()

    def save_cup(self, *, capture_id, cup_id, name):
        def save():
            self.session._check_selection(capture_id)
            record = self.named_cups.save(name, self.session.inventory, cup_id)
            return {"kind": "saved_cup", "cup": record,
                    "message": f"Saved {record['name']} with a local reference photo. No robot command sent. "
                               f"Later use: pick saved {record['name']}"}
        return self._edit_saved(save)

    def forget_cup(self, cup_id):
        def forget():
            record = self.named_cups.delete(cup_id)
            return {"kind": "saved_cup", "message": f"Forgot {record['name']}. No robot command sent."}
        return self._edit_saved(forget)

    def named_command(self, text):
        """Explicit named commands never let a model invent or overwrite a name."""
        text = " ".join(text.strip().split())
        match = re.fullmatch(r"(?:save|remember) (?:cup )?([1-9][0-9]*) as (.+)", text, re.I)
        if match:
            capture = self.session.inventory["capture"] if self.session.inventory else ""
            return self.save_cup(capture_id=capture, cup_id=int(match[1]), name=match[2])
        if text.lower() in ("saved cups", "list saved cups"):
            records = self._require_library().list()
            return {"kind": "saved_cups", "message": "Saved cups: " +
                    (", ".join(r["name"] for r in records) if records else "none yet.")}
        match = re.fullmatch(r"(pick(?: up)?|forget) saved(?: cup)? (.+)", text, re.I)
        if match:
            record = self._require_library().get(match[2])
            return self.forget_cup(record["id"]) if match[1].lower() == "forget" else self.propose_saved(record["id"])
        # Familiar short form only when the entire name exactly matches a saved
        # entry. Negations, conditionals and arbitrary requests still use the
        # existing intent parser; they cannot accidentally trigger this shortcut.
        match = re.fullmatch(r"pick(?: up)? (.+)", text, re.I)
        if (match and self.named_cups is not None
                and not re.fullmatch(r"(?:cup )?[0-9]+|all(?: available)?(?: cups)?", match[1], re.I)):
            names = {r["name"].casefold(): r for r in self.named_cups.list()}
            if match[1].casefold() in names:
                return self.propose_saved(names[match[1].casefold()]["id"])
        return None

    @staticmethod
    def _reference_binding(record):
        return {key: record[key] for key in
                ("id", "name", "saved_at", "capture_id", "cup_id", "reference_sha256")}

    def _bound_reference(self, binding):
        current = self._require_library().get_id(binding["id"])
        if self._reference_binding(current) != {key: binding[key] for key in self._reference_binding(current)}:
            raise SelectionError("The saved cup reference changed. Request a new preview.")
        return current

    def _visual_match(self, reference, inventory, *, audit=None, stage="proposal"):
        from named_cups import candidate_images
        if self.cup_matcher is None:
            raise SelectionError("Visual cup matching requires --llm and a local vision model.")
        before = inventory_fingerprint(inventory)
        capture_id = inventory["capture"]
        candidates = candidate_images(inventory, self.named_cups.image_root)
        result = self.cup_matcher.match(reference["image_bytes"], candidates)
        candidate_ids = [candidate["cup_id"] for candidate in candidates]
        valid = (isinstance(result, dict) and set(result) == {"status", "cup_id", "reason"}
                 and isinstance(result["status"], str)
                 and result["status"] in ("match", "ambiguous", "not_found")
                 and isinstance(result["reason"], str) and len(result["reason"]) <= 80
                 and not any(ord(character) < 32 or 127 <= ord(character) <= 159
                             for character in result["reason"])
                 and ((result["status"] == "match" and type(result["cup_id"]) is int
                       and result["cup_id"] in candidate_ids)
                      or (result["status"] != "match" and result["cup_id"] is None)))
        outcome = dict(result) if valid else {
            "status": "invalid_response", "cup_id": None,
            "reason": "Visual matcher returned an invalid response."}
        if audit is not None:
            # Only bounded, validated selection facts enter the audit. Never
            # retain model payloads, image bytes, or arbitrary result fields.
            audit.setdefault("visual_matches", []).append({
                "stage": stage, "capture_id": capture_id,
                "candidate_ids": candidate_ids, **outcome})
        self.session._check_stop()
        if before != inventory_fingerprint(inventory):
            raise SelectionError("The scan changed during visual matching. Request a new preview.")
        if not valid or outcome["status"] != "match":
            explanation = {
                "ambiguous": "the visual matcher could not establish one unique match",
                "not_found": "the visual matcher found no matching candidate",
                "invalid_response": "the visual matcher returned an invalid result",
            }[outcome["status"]]
            raise SelectionError(f"Cannot uniquely identify saved cup {reference['name']}: "
                                 f"{outcome['status']} — {explanation}. "
                                 f"Visual detail: {outcome['reason']} "
                                 "No movement requested. Review the scan and use a numbered cup if needed.")
        return result

    def propose_saved(self, cup_id):
        return self._propose("Operator requested a saved cup", saved_id=cup_id)

    def _identity_check(self, binding, *, audit=None):
        def check(fresh, target):
            reference = self._bound_reference(binding)
            match = self._visual_match(reference, fresh, audit=audit, stage="confirmation")
            self._bound_reference(binding)
            if match["cup_id"] != target["id"]:
                raise SelectionError("The saved cup no longer matches the selected position. Request a new preview.")
            return {"saved_cup_id": binding["id"], "name": binding["name"],
                    "reference_sha256": binding["reference_sha256"], "matched_cup_id": target["id"],
                    "reason": match["reason"]}
        return check

    def start(self):
        """A fresh scan and batch preview, never a RAPID start or automatic pick."""
        return self._propose("Operator requested start picking", {
            "action": "start", "selector": "none", "cup_id": None,
            "message": "Scan now and preview a pick-all batch for confirmation.",
        })

    def status(self):
        state = self.session.status_summary()
        message = (f"Application status: software stop {'latched' if state['stop_requested'] else 'not requested'}; "
                   f"recovery {'required' if state['recovery_required'] else 'not required'}; "
                   f"scan {'ready' if state['scan_usable'] else 'required before movement'}. "
                   "This is application status, not a fresh physical robot measurement.")
        return {"kind": "status", "status": state, "message": message}

    @staticmethod
    def help():
        return {"kind": "help", "message": COMMAND_HELP}

    def propose_batch(self, *, capture_id):
        return self._propose("Operator selected pick all available cups", {
            "action": "goto_all", "selector": "none", "cup_id": None,
            "message": "Explicit operator batch selection.",
        }, capture_id=capture_id)

    def _propose(self, text, operator_intent=None, capture_id=None, saved_id=None):
        if not self._lock.acquire(blocking=False):
            raise SelectionError("A language request is already running; no request was queued.")
        record = {"request_id": uuid.uuid4().hex, "text": text, "state": "interpreting"}
        try:
            self._cancel()
            if saved_id is not None:
                reference = self._require_library().get_id(saved_id)
                record["requested_saved_cup"] = self._reference_binding(reference)
                if self.cup_matcher is None:
                    raise SelectionError("Restart with --llm to identify a saved cup using the local vision model.")
                self.session._check_stop()
                if self.session.motion_uncertain:
                    raise SelectionError("Inspect the robot and acknowledge recovery before requesting a saved cup.")
                settings_before = self.session.motion_settings()
                fresh = self.session.scan()
                record["capture_id"] = fresh["capture"]
                match = self._visual_match(reference, fresh, audit=record)
                if self.session.inventory is not fresh:
                    raise SelectionError("The active scan changed during visual matching. Request a new preview.")
                if settings_before != self.session.motion_settings():
                    raise SelectionError("Movement settings changed during visual matching. Request a new preview.")
                binding = self._reference_binding(reference)
                self._bound_reference(binding)
                record["saved_cup"] = {**binding, "match_reason": match["reason"]}
                operator_intent = {"action": "goto", "selector": "id", "cup_id": match["cup_id"],
                                   "message": "Visual match to the operator's saved reference; confirm its identity."}
                capture_id = fresh["capture"]
            if operator_intent is not None:
                if operator_intent["action"] not in ("goto", "hover", "goto_all", "start"):
                    raise SelectionError("Choose pick and transfer or hover.")
                if operator_intent["action"] == "start":
                    pass
                elif operator_intent["action"] == "goto_all":
                    if self.session.inventory is None or self.session.inventory["capture"] != capture_id:
                        raise SelectionError("Capture ID is not the active scan. Run scan again.")
                else:
                    selected_cup(self.session.inventory, capture_id, operator_intent["cup_id"])
            scene = scene_summary(self.session)
            record["capture_id"] = scene["capture_id"]
            fingerprint = inventory_fingerprint(self.session.inventory)
            motion_settings = self.session.motion_settings()
            intent = validate_intent(operator_intent if operator_intent is not None else
                                     self.interpreter.interpret(text, scene))
            record["intent"] = intent
            if fingerprint != inventory_fingerprint(self.session.inventory):
                raise SelectionError("The scan changed while interpreting. Repeat your request using the current scan.")
            if motion_settings != self.session.motion_settings():
                raise SelectionError("Movement settings changed while interpreting. Request a new preview.")
            action = intent["action"]
            if action in ("status", "help"):
                record["state"] = "completed"
                self._save(record)
                return self.status() if action == "status" else self.help()
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
            if action == "start":
                if self.session.motion_uncertain or self.session.stop_requested:
                    raise SelectionError("Recovery is required before starting. Inspect the robot and acknowledge recovery first.")
                self.session.scan()
                scene = scene_summary(self.session)
                fingerprint = inventory_fingerprint(self.session.inventory)
                record["capture_id"] = scene["capture_id"]
                action = "goto_all"
            target = None if action == "goto_all" else self._target(intent)
            if action == "show":
                record["state"] = "completed"
                self._save(record)
                return {"kind": "cup", "cup": target}
            if self.session.motion_uncertain:
                raise SelectionError("Recovery is required. Check the robot; only the operator can use reset.")
            if not self.session.scan_usable:
                raise SelectionError("Run scan before requesting a new movement.")
            preview = (self.session.preview_batch(capture_id=scene["capture_id"]) if action == "goto_all" else
                       self.session.preview_selection(action=action, capture_id=scene["capture_id"], cup_id=target["id"]))
            cup_ids = preview["cup_ids"]
            if motion_settings != self.session.motion_settings():
                raise SelectionError("Movement settings changed. Request a new preview.")
            description = ("pick with the left arm, hand to the right, place, and return both arms home"
                           if action in ("goto", "goto_all") else "move the left arm above it without gripping")
            if self.session.left_only and action in ("goto", "goto_all"):
                description = "pick, lift and replace at the original position, then return the left arm home"
            placement = preview["placement"]
            destinations = ""
            if placement:
                destinations = " Destinations: " + ", ".join(
                    f"cup {cup_id} → location {place['id']} ({place['x']:g}, {place['y']:g}, {place['z']:g} mm)"
                    for cup_id, place in zip(cup_ids, placement["locations"])) + "."
            excluded = preview.get("excluded_cup_ids", [])
            capacity = (f" No free location for cups {', '.join(map(str, excluded))}; they are excluded."
                        if excluded else "")
            record.update(state="proposed", action=action, cup_id=target["id"] if target else None,
                          cup_ids=cup_ids, placement=placement, excluded_cup_ids=excluded,
                          speed=self.session.speed, flip_speed=min(self.session.speed, FLIP_SPEED),
                          home_speed=self.session.home_speed,
                          motion_settings=motion_settings,
                          handover=(motion_settings["handover"]
                                    if action in ("goto", "goto_all") and not self.session.left_only else None),
                          description=description + "." + destinations + capacity)
            token = record["request_id"][:8]
            self._save(record)  # A proposal is unusable unless its audit was saved.
            if self.session.stop_requested:
                raise SelectionError("Software stop was requested. This proposal is cancelled; inspect the robot before recovery.")
            self.pending = {"token": token, "created": self.clock(), "record": record,
                            "fingerprint": fingerprint}
            mode = "Preview only" if self.session.robot.dry_run else "Physical movement"
            alignment_text = ""
            if record["handover"] is not None:
                h = record["handover"]
                alignment_text = ("Right receiving XYZ: " + ", ".join(f"{h['receive_mm'][a]:g}" for a in "xyz")
                                  + " mm; offsets: " + ", ".join(f"{h['offset_mm'][a]:+g}" for a in "xyz")
                                  + f" mm; receiving approach up to {h['approach_speed']:g} mm/s.\n")
            named_text = (f"Saved cup: {record['saved_cup']['name']}. Visual match: {record['saved_cup']['match_reason']}\n"
                          "Compare the reference photo with the highlighted cup before confirming.\n"
                          if "saved_cup" in record else "")
            home_text = (f"Automatic home: {record['home_speed']:g} mm/s.\n"
                         if action in ("goto", "goto_all") else "")
            return {"kind": "proposal", "token": token,
                    "message": f"{mode}: cup(s) {', '.join(map(str, cup_ids))} — {record['description']}\n"
                               f"Normal speed: {self.session.speed:g} mm/s; cup turning: {record['flip_speed']:g} mm/s, 30% acceleration.\n"
                               f"{home_text}{alignment_text}{named_text}"
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
                if record["motion_settings"] != self.session.motion_settings():
                    raise SelectionError("Movement settings changed. Request a new preview before moving.")
                if "saved_cup" in record:
                    self._bound_reference(record["saved_cup"])
                record["state"] = "confirmed"
                self._save(record)
                if record["action"] == "goto_all":
                    jobs = self.session.execute_batch(capture_id=record["capture_id"],
                        cup_ids=record["cup_ids"], placement=record["placement"], expected_motion=record["motion_settings"])
                else:
                    options = {"placement": record["placement"]} if record["action"] == "goto" else {}
                    if "saved_cup" in record:
                        options["identity_check"] = self._identity_check(record["saved_cup"], audit=record)
                    jobs = self.session.execute_selection(action=record["action"],
                        capture_id=record["capture_id"], cup_id=record["cup_id"],
                        expected_motion=record["motion_settings"], **options)
                record["state"] = "previewed" if self.session.robot.dry_run else "completed"
                self._save(record)
                return {"kind": "result", "action": record["action"], "jobs": jobs,
                        "message": "Preview complete; no robot commands sent." if self.session.robot.dry_run else
                        (("Robot reported the cup replaced and the left arm home. Scan again."
                          if self.session.left_only else
                          "Robot reported pick, handover and placement complete; home poses checked. Scan again.")
                         if record["action"] in ("goto", "goto_all") else "Robot reported hover complete. Scan again.")}
            except BaseException as error:
                record.update(state="blocked_or_interrupted", error=str(error))
                try:
                    self._save(record)
                except OSError:
                    pass
                raise
        finally:
            self._lock.release()
