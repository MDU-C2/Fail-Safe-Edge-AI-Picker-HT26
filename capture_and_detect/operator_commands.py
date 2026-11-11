"""Shared explicit operator commands; these never need a language model."""

START_COMMANDS = frozenset(("start", "start picking"))
STOP_COMMANDS = frozenset(("stop", "halt", "stop robot", "stop the robot"))

COMMAND_HELP = """Commands:
  start / start picking   Scan now and preview a pick-all batch; confirmation is required.
  stop / halt             Block further commands and request a software stop of both arms.
  status                  Show this application's current state; no robot request.
  help                    Show these commands.
  scan                    Detect cups without moving.
  list / available cups   Show the saved detections.
  goto 2 / pick cup 2      Select one cup for pick, handover, placement and home.
  goto all                Preview available cups and destinations as one batch.
  hover 2                 Move above one cup without gripping.
  save cup 2 as Kasun mug  Save a named visual reference from the current scan; no movement.
  saved cups              List locally saved cup references.
  pick saved Kasun mug    Scan, visually match, and preview a pick for confirmation.
  forget saved Kasun mug  Remove a saved reference; no movement.
  cancel                  Discard a pending proposal only.
  confirm <code>          Confirm the exact displayed terminal proposal.
  reset                   Acknowledge inspection after a stop/failure; no movement.

Web movement always needs its confirmation button. Exact terminal goto/hover
commands execute directly. Software stop depends on the gateway and controller;
use the robot's physical emergency stop when immediate stopping is needed.
After Stop: inspect/recover using the robot controls, acknowledge recovery, then
scan again. Start does not restart RAPID or resume an interrupted movement.
"""
