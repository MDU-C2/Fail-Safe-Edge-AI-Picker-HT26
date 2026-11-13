"""Explicit operator actions, separate from cup selection and language interpretation."""

import copy
import math
import secrets
import time

from cup_inventory import SelectionError
from handover_plan import HOME_SPEED
from robot_client import RobotClient


def valid_speed(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 1 <= value <= 1500):
        raise SelectionError("Speed must be a finite number from 1 to 1500 mm/s.")
    return float(value)


def control_plan(action, arm, speed):
    """Build only allowlisted jobs; never accept coordinates or raw endpoints."""
    speed = valid_speed(speed)
    jobs = []
    if action in ("home", "open", "grip"):
        if arm not in ("left", "right", "both"):
            raise SelectionError("Choose the left arm, right arm, or both arms.")
        arms = (("right", "left") if action == "home" else ("left", "right")) if arm == "both" else (arm,)
        for target in arms:
            body = {"arm": target, "wait": True}
            if action == "home":
                body.update(speed=min(speed, 100), acceleration=100, ramp=100)
                endpoint = RobotClient.HOME_ENDPOINT
            else:
                body["action"] = action
                endpoint = RobotClient.GRIPPER_ENDPOINT
            jobs.append({"name": f"operator_{target}_{action}", "endpoint": endpoint, "body": body})
        target = f"{arms[0]} arm, then {arms[1]} arm" if arm == "both" else f"{arm} arm"
        if action == "home":
            description = (f"Move the {target} to its configured home joint position at up to "
                           f"{min(speed, 100):g} mm/s. Inspect the whole return path first. "
                           "This does not plan an escape from a collision.")
        else:
            description = (f"{'Open' if action == 'open' else 'Close'} the gripper on the {target}. "
                           + ("An object being held may fall; support it first." if action == "open" else
                              "Keep fingers and loose objects out of the gripper."))
    elif action in ("rapid_reset", "rapid_start", "collision_ack"):
        if arm is not None:
            raise SelectionError("Controller actions apply to both arms; arm must be null.")
        description = {
            "rapid_reset": "Stop RAPID, discard pending arm commands and reset the program pointer to main. "
                           "Leave execution stopped. This does not acknowledge a collision or move home.",
            "rapid_start": "Discard pending arm commands, reset to main and initialize RAPID. "
                           "The arm programs may calibrate the grippers. Motors must already be on in AUTO. "
                           "This does not resume the interrupted pick or move home.",
            "collision_ack": "Request controller collision acknowledgement after inspection. "
                             "ABB may require acknowledgement on the FlexPendant because of client privileges. "
                             "This does not enable motors, start RAPID, or move home.",
        }[action]
    else:
        raise SelectionError("Unknown robot control action.")
    return {"action": action, "arm": arm, "speed": min(speed, 100) if action == "home" else None,
            "description": description, "jobs": jobs}


class RobotControls:
    """Single-worker proposals, consumed before execution and invalidated by speed changes."""

    def __init__(self, session, *, clock=time.monotonic, ttl=60):
        self.session = session
        self.clock = clock
        self.ttl = ttl
        self.pending = None
        self.status = None

    def cancel(self):
        self.pending = None

    def snapshot(self):
        proposal = None
        if self.pending:
            proposal = {key: copy.deepcopy(self.pending[key]) for key in
                        ("token", "action", "arm", "speed", "description")}
            proposal["expires_in"] = max(0, self.ttl - (self.clock() - self.pending["created"]))
        return {"status": copy.deepcopy(self.status), "proposal": proposal,
                "home_speed": min(self.session.speed, 100), "capabilities": self.capabilities()}

    def capabilities(self):
        capabilities = getattr(self.session.robot, "operator_capabilities", None)
        if isinstance(capabilities, dict):
            return copy.deepcopy(capabilities)
        # Custom library robots must explicitly advertise real operator actions.
        # Simulation has no external endpoint and may preview all controls.
        supported = self.session.robot.dry_run is True
        return {action: {"supported": supported if supported or action != "robot_status" else None,
                        "reason": None if supported else
                "Robot control support has not been read. Refresh robot status."}
                for action in ("home", "open", "grip", "robot_status", "stop", "rapid_reset",
                               "rapid_start", "collision_ack", "collision_status")}

    def _require_supported(self, action):
        capability = self.capabilities().get(action, {})
        if capability.get("supported") is not True:
            raise SelectionError(capability.get("reason") or "This action is not supported by the connected gateway.")
        return capability

    def _check_ready(self):
        self.session._check_stop()
        if self.session.motion_uncertain:
            raise SelectionError("Inspect both arms and the cup, then acknowledge app recovery before robot controls.")

    def set_speed(self, speed):
        self.cancel()
        self._check_ready()
        self.session.set_speed(valid_speed(speed))
        return {"message": f"Future cup actions use {self.session.speed:g} mm/s; automatic home uses "
                           f"{getattr(self.session, 'home_speed', min(self.session.speed, HOME_SPEED)):g} mm/s; manual home is capped at "
                           f"{min(self.session.speed, 100):g} mm/s. Pending selections cancelled. "
                           "This setting lasts until the app restarts."}

    def refresh_status(self):
        # Discard a prior successful snapshot on failure, rather than displaying
        # it as fresh after losing the gateway connection.
        self.status = None
        refresh = getattr(self.session.robot, "refresh_operator_capabilities", None)
        if callable(refresh):
            refresh()
        self._require_supported("robot_status")
        self.status = self.session.robot.read_control_status()
        return {"message": "Robot status refreshed." if not self.session.robot.dry_run else
                           "Simulated robot status; no gateway request sent."}

    def set_handover_offset(self, offset):
        self.cancel()
        self._check_ready()
        self.session.set_handover_offset(offset)
        target = self.session.motion_settings()["handover"]["receive_mm"]
        return {"message": "Handover alignment applied for future picks: right receiving XYZ "
                           + ", ".join(f"{target[a]:g}" for a in "xyz")
                           + " mm. No robot command sent. Scan again and review the new pick preview. "
                           "Alignment resets to zero when the app restarts."}

    def propose(self, action, arm):
        self.cancel()
        self._check_ready()
        capability = self._require_supported(action)
        plan = control_plan(action, arm, self.session.speed)
        self.pending = {**plan, "token": secrets.token_hex(8), "created": self.clock(),
                        "session_speed": self.session.speed, "capability": capability}
        return {"message": "Review the robot control action and acknowledge inspection before confirming."}

    def confirm(self, token):
        pending, self.pending = self.pending, None
        if not pending or not secrets.compare_digest(token, pending["token"]):
            raise SelectionError("This robot control confirmation no longer matches. Preview the action again.")
        if self.clock() - pending["created"] >= self.ttl:
            raise SelectionError("Robot control preview expired. Preview the action again.")
        self._check_ready()
        capability = self._require_supported(pending["action"])
        if capability != pending["capability"]:
            raise SelectionError("Gateway support changed. Review the robot action again.")
        if self.session.speed != pending["session_speed"]:
            raise SelectionError("Speed changed. Preview the action again.")
        self.status = None
        result = self.session.execute_control(pending["action"], pending["arm"])
        # Command-specific confirmation may include a newly observed snapshot.
        self.status = copy.deepcopy(result.get("status"))
        return result
