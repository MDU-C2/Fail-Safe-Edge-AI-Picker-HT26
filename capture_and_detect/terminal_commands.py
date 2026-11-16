"""Shared command-line handlers; no image or machine-learning imports."""
import argparse
import json
import math
import shlex
from cup_inventory import SelectionError
from handover_plan import pick_position_problem
from operator_commands import START_COMMANDS, STOP_COMMANDS

HELP = """Commands:
  start               Scan now and preview a pick-all batch; then confirm it.
  stop / halt         Block further jobs and request a software stop of both arms.
  status              Show application state without contacting the robot.
  Enter / scan        Detect and save cups; no robot movement.
  list                Show cups, saved XYZ, and the corrected hover target.
  show 2              Show all stored information for cup 2.
  goto 2              Verify cup 2; pick with left, hand to right, place it,
                      then finish with both arms home.
  hover 2             Verify cup 2; move the left arm above it without gripping.
  goto all            Preview a batch of available cups, limited by free slots.
  save cup 2 as mug    Save a named visual reference; no movement.
  saved cups          List saved references.
  pick saved mug      Scan, match the reference, then require confirmation (--llm).
  forget saved mug    Remove that saved reference; no movement.
  confirm <code>      Execute the displayed batch or language proposal once.
  cancel              Discard a proposal; does not stop an active movement.
  reset               Acknowledge checking the robot after an unconfirmed move.
                      Sends nothing. A new scan is still required.
  help                Show this help.
  q / quit            Exit; request Stop first if a command is running.
IDs belong to one scan and may change after scanning again.
Stop remains responsive while a command runs. A software stop depends on the
gateway and controller; use the robot's physical emergency stop when needed.
After Stop, inspect/recover using the robot controls, then reset and scan.
Start never restarts RAPID or resumes an interrupted movement.
"""

LANGUAGE_HELP = """Local language mode:
  Type a sentence, for example: pick cup two / hover above the rightmost cup.
  A movement proposal shows a cup ID and a one-use confirmation code.
  confirm <code>      Execute that exact proposal through the existing checks.
  cancel             Discard a pending proposal; does not stop a moving robot.
  Exact terminal commands (scan, goto 2, hover 2, reset, etc.) still work directly.
Left/right refer to the numbered camera image. Use saved names for visual cup
matching; arbitrary color selectors and gestures are not supported.
"""

def log(*parts):
    print(*parts, flush=True)

def print_cups(inventory):
    if inventory is None:
        log("No scan in this session. Use scan first.")
        return
    log(f"Capture: {inventory['capture']}")
    log(" ID  Conf     Saved robot X/Y/Z (mm)      Hover flange X/Y/Z (mm)       Status")
    for cup in inventory["cups"]:
        def coordinates(position):
            return " ".join(f"{position[a]:7.1f}" if position.get(a) is not None else "      ?" for a in ("x", "y", "z"))
        hover = cup.get("hover_target")
        corrected = coordinates(hover["flange_mm"]) if hover else "      --       --       --"
        confidence = f"{cup['confidence']:.2f}" if cup["confidence"] is not None else "   ?"
        status = cup["blocked_reason"] or cup["status"]
        if cup["selectable"]:
            pick_problem = pick_position_problem(cup, inventory.get("tag_offset_mm", (0, 0, 0)))
            if pick_problem:
                status = f"hover only: {pick_problem}"
        log(f" {cup['id']:2d}  {confidence}  {coordinates(cup)}    {corrected}    {status}")
    if not inventory["cups"]:
        log("No cups detected.")
    label = "Capture image" if inventory["images"].get("annotation_status") else "Numbered image"
    log(f"{label}: {inventory['images']['annotated']}")
    observation = inventory.get("placement_observation")
    if observation:
        log(observation["message"])
        if observation.get("released_ids"):
            log("Placement locations released:", ", ".join(map(str, observation["released_ids"])))

def handle_terminal(session, line):
    """Only explicit goto/hover with a valid cup ID can request movement."""
    parts = shlex.split(line)
    command, values = (parts[0].lower(), parts[1:]) if parts else ("scan", [])
    if command in ("q", "quit", "exit") and not values:
        return False
    if command in ("scan", "capture") and not values:
        print_cups(session.scan())
    elif command == "list" and not values:
        print_cups(session.inventory)
    elif command == "help" and not values:
        log(HELP)
    elif command == "reset" and not values:
        session.acknowledge_recovery()
        log("Recovery acknowledged. No command sent. Use scan before selecting another cup.")
    elif command in ("show", "goto", "hover") and len(values) == 1:
        try:
            cup_id = int(values[0])
        except ValueError:
            raise SelectionError("Use a numeric cup ID, e.g. goto 2.") from None
        if session.inventory is None:
            raise SelectionError("Use scan first.")
        if command == "show":
            matches = [cup for cup in session.inventory["cups"] if cup["id"] == cup_id]
            if not matches:
                raise SelectionError(f"Cup {cup_id} is not in the current scan.")
            log(json.dumps(matches[0], indent=2))
        else:
            log(f"Rechecking cup {cup_id} against a new camera observation ...")
            session.execute_selection(action=command, capture_id=session.inventory["capture"], cup_id=cup_id)
            if session.robot.dry_run:
                log("Preview complete; no robot commands sent.")
            elif command == "goto":
                log("Robot reported the cup replaced and the left arm home. Scan again before another cup."
                    if session.left_only else
                    "Robot reported the pick, handover and placement complete; both home poses checked. Scan again before another cup.")
            else:
                log("Robot reported DONE above the selected cup. Scan again before another move.")
    else:
        raise SelectionError("Unknown command. Use scan, list, show <id>, goto <id>, hover <id>, reset, help, or quit.")
    return True

def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return value

def handle_input(session, line, language=None):
    """Keep operator commands separate from untrusted language interpretation."""
    command = " ".join(line.strip().lower().split())
    if command in STOP_COMMANDS:
        try:
            session.latch_stop()
        except OSError as error:
            log(f"Stop recovery state could not be saved: {error}")
        result = session.request_stop()
        if language is not None:
            try:
                language.cancel()
            except SelectionError:
                # Interactive operation uses TerminalController to invalidate
                # the proposal when its ordinary worker exits.
                pass
        log(result["message"])
        if not result.get("dry_run") and not result.get("stopped"):
            log("Robot stop is unconfirmed. Use the physical robot controls; recovery remains required.")
        return True
    if language is None:
        if command == "status":
            log(json.dumps(session.status_summary(), indent=2))
            return True
        return handle_terminal(session, line)
    words = line.strip().split()
    first = words[0].lower() if words else ""
    if command in START_COMMANDS:
        result = language.start()
    elif command == "status":
        result = language.status()
    elif command == "help":
        log(language.help()["message"])
        return True
    elif command in ("goto all", "pick all", "pick all available cups"):
        result = language.propose_batch(capture_id=session.inventory["capture"] if session.inventory else "")
    elif first == "confirm":
        if len(words) != 2:
            raise SelectionError("Type confirm followed by the displayed proposal code.")
        result = language.confirm(words[1])
    elif first == "cancel":
        if len(words) != 1:
            raise SelectionError("Use cancel to discard a proposal. It does not stop robot motion.")
        language.cancel()
        log("Pending proposal cancelled. This does not stop an already dispatched robot movement.")
        return True
    elif (not words or (len(words) == 1 and first in
          ("scan", "capture", "list", "help", "q", "quit", "exit", "reset"))
          or (len(words) == 2 and first in ("show", "goto", "hover") and words[1].isdigit())):
        if first not in ("list", "show", "help"):
            language.cancel()
        keep_running = handle_terminal(session, line)
        if first == "help":
            log(LANGUAGE_HELP)
        return keep_running
    elif first in ("reset", "home"):
        raise SelectionError("Recovery and home are not language actions. Use the documented operator recovery procedure.")
    else:
        log("Interpreting locally ...")
        result = language.propose(line)
    if result["kind"] == "inventory":
        print_cups(result["inventory"])
    elif result["kind"] == "cup":
        log(json.dumps(result["cup"], indent=2))
    else:
        if result["kind"] in ("reply", "clarify"):
            log("LLM response — no robot action requested:")
        log(result["message"])
    return True
