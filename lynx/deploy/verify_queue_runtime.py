#!/usr/bin/env python3
"""Offline installed-runtime verification; no model, camera or robot is opened.

Run with the packaged Python, for example:
  /opt/venv/bin/python /tmp/verify_queue_runtime.py --app-root /app

All state lives in a temporary directory. Real HTTP/socket access is blocked;
the actual queue client sees only the small in-memory gateway below. No pytest,
NumPy, OpenCV, inference worker, model or target state directory is required.
"""
import argparse
from contextlib import ExitStack, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.abc
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch


HEAVY_MODULES = {"numpy", "cv2", "torch", "ultralytics", "depthai", "fastapi", "uvicorn", "gateway"}
API = "http://offline.invalid/api/v1"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *_args):
        if fullname.split(".", 1)[0] in HEAVY_MODULES:
            raise AssertionError("Heavy dependency imported: " + fullname)


class Response:
    def __init__(self, value, status=200):
        self.value, self.status_code, self.headers = copy.deepcopy(value), status, {}
        self.closed = False

    def json(self):
        return copy.deepcopy(self.value)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("Offline HTTP " + str(self.status_code), response=self)

    def close(self):
        self.closed = True


class FakeGateway:
    """Only the installed queue/control read contracts; never opens a socket."""
    def __init__(self, robot_type):
        self.robot_type = robot_type
        self.owner = None
        self.client_id, self.priority = "tertiary", 100
        self.acquired_at = None
        self.queue = {"state": "idle", "length": 0, "steps": []}
        self.calls, self.submissions, self.responses = [], [], []
        self.submission_error = None

    def response(self, value, status=200):
        result = Response(value, status)
        self.responses.append(result)
        return result

    def path(self, method, url):
        require(url.startswith(API + "/"), "Unexpected mock gateway address")
        path = url[len(API):]
        self.calls.append((method, path))
        return path

    def lease(self):
        now = datetime.now(timezone.utc)
        if self.acquired_at is None:
            self.acquired_at = now.isoformat()
        return {"active": True, "client_id": self.client_id, "priority": self.priority,
                "acquired_at": self.acquired_at, "expires_at": (now + timedelta(seconds=5)).isoformat()}

    def post(self, url, **options):
        path = self.path("POST", url)
        if path == "/control/acquire":
            if self.owner and self.owner["priority"] >= self.priority:
                return self.response({"detail": "Control owned"}, 409)
            self.owner = {"active": True, "client_id": self.client_id, "priority": self.priority}
            return self.response({"success": True, "lease": self.lease()})
        if path == "/control/renew":
            require(self.owner is not None, "Renewal without a held lease")
            return self.response({"success": True, "lease": self.lease()})
        if path == "/control/release":
            self.owner = None
            return self.response({"success": True, "lease": {"active": False}})
        require(path == "/robot/rws/queue", "A per-step motion command escaped the queue")
        require(self.owner is not None, "Queue submitted without control")
        steps = copy.deepcopy(options["json"]["steps"])
        self.submissions.append(steps)
        self.queue = {"state": "done", "owner": self.client_id, "length": len(steps),
            "started_at": datetime.now(timezone.utc).isoformat(), "message": "DONE",
            "cancel_requested": False,
            "steps": [{"name": step["name"], "type": step["type"], "arm": step["body"]["arm"],
                       "group": step["group"], "state": "done", "message": "DONE"} for step in steps]}
        if self.submission_error is not None:
            raise self.submission_error
        return self.response({"accepted": True}, 202)

    def get(self, url, **_options):
        path = self.path("GET", url)
        if path == "/control":
            return self.response(self.owner or {"active": False})
        if path == "/robot/rws/queue":
            return self.response(self.queue)
        if path.startswith("/robot/rws/status?arm="):
            return self.response({"accepted": True, "message": "DONE"})
        if path.endswith("/joints"):
            arm = path.split("/")[-2]
            return self.response({"arm": arm, "joints": list(self.robot_type.HOME_JOINTS),
                                  "arm_angle": self.robot_type.HOME_ARM_ANGLES[arm]})
        raise AssertionError("Unexpected mock GET: " + path)


def scene(grid, sequence, sources, occupied):
    from cup_inventory import build_inventory
    from placement_records import combine
    objects = []
    for u, x in sources:
        for kind, radius in (("CUP", 15), ("CUP BASE", 3)):
            objects.append({"class": kind, "confidence": 0.9, "detection_id": len(objects),
                "x_px": u, "y_px": 100, "bbox_xyxy": [u - radius, 80, u + radius, 120],
                "robot_x_mm": x, "robot_y_mm": 100, "robot_z_mm": 50,
                "x_mm": 10, "y_mm": 20, "z_mm": 800, "diameter_mm": 50})
    inventory = build_inventory(objects, capture_id=f"offline-{sequence}",
        frame={"sequence": sequence, "timestamp": "2026-11-20T00:00:00Z",
               "observed_at": "2026-11-20T00:00:00Z"}, calibration_id="offline-calibration",
        gateway=API, images={}, model={"weights": "offline-no-model"})
    snapshot = grid.snapshot()
    slots = [{"id": place["id"], "state": "occupied" if occupied else "clear",
              "reason": "Offline synthetic evidence", "reason_code": "raised_depth" if occupied else "clear",
              "valid_fraction": 1.0, "table_fraction": 1.0, "visible_fraction": 1.0,
              "floor_visible_fraction": 1.0, "polygon_px": [[10, 200], [30, 200], [30, 220], [10, 220]],
              "cup_count": 0, "assigned_cups": [], "evidence": ["raised_depth"] if occupied else []}
             for place in snapshot["locations"]]
    inventory["placement_observation"] = combine(snapshot, [slots], [sequence])
    inventory["source_presence"] = {"cup_detections": len(sources)}
    return inventory


def verify_finite_queue_batch(root, gateway):
    from cup_commands import CupSession
    from cup_inventory import atomic_json
    from handover_plan import handover_jobs, right_via_pose
    from placing_grid import PlacingGrid
    from queued_robot_client import QueueRobotClient, plan_to_queue_steps
    from recovery_journal import RecoveryJournal
    from robot_client import RobotClient

    grid_path = root / "grid.json"
    grid_path.write_text(json.dumps({"cell_size_mm": 90, "locations": [
        {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False}]}))
    grid, recovery = PlacingGrid(grid_path), RecoveryJournal(root / "recovery.json")
    # Initial two sources; the second moves and a third is added. A full grid
    # forces one stationary check, then the same camera-cleared cell is reused.
    frames = [([(100, 450), (220, 520)], False),
              ([(220, 560), (340, 650)], True),
              ([(220, 560), (340, 650)], False),
              ([(340, 650)], False), ([], False)]
    captures, waits = [], []
    def capture(purpose):
        require(len(captures) < len(frames), "Finite batch captured again after completion")
        require(purpose == ("scan" if not captures else "between_cups_grid"), "Wrong capture purpose")
        if captures:
            require(gateway.queue["state"] == "done", "Camera ran before queue completion")
            require(any(step["name"].endswith("right_park") for step in gateway.queue["steps"]),
                    "Camera ran before the existing right waiting pose")
            require(not recovery.required(), "Confirmed movement still has an unresolved journal")
        sources, occupied = frames[len(captures)]
        captures.append(purpose)
        return scene(grid, 10 + len(captures), sources, occupied), 100.0 + len(captures)

    client = QueueRobotClient(API, {"Authorization": "Bearer offline-test-credential"})
    session = CupSession(capture, lambda value: atomic_json(root / "cups.json", value), client,
        "offline-calibration", placing_grid=grid, recovery=recovery, between_cup_check=True,
        between_cup_grid_check=True, continuous_batch=False, right_park_pose=right_via_pose())
    def wait_once():
        require(session.batch_progress["state"] == "waiting_for_grid", "Unexpected idle cup monitoring")
        require(not any(step["type"] == "home" for step in gateway.queue["steps"]),
                "Home occurred while source cups remained")
        waits.append(len(gateway.submissions))
    session._wait_for_grid = wait_once
    require(client.try_begin_standby("tertiary", 100), "Could not acquire mock tertiary lease")
    try:
        session.scan()
        initial = session.inventory["capture"]
        reference_plan = handover_jobs(session.inventory["cups"][0],
            place=grid.snapshot()["locations"][0], right_park_pose=right_via_pose())
        queue_steps = plan_to_queue_steps(reference_plan)
        groups = {step["name"]: int(step["group"][1:]) for step in queue_steps}
        arms = {job["name"]: job["body"]["arm"] for job in reference_plan}
        for job in reference_plan:
            for dependency in job["after"]:
                require(groups[job["name"]] >= groups[dependency] + (arms[job["name"]] != arms[dependency]),
                        "Queue dropped a shared motion barrier")
        require(groups["left_clear"] == groups["right_lower"],
                "The arms must separate together before turnover")
        require(groups["right_retreat"] > groups["left_clear"],
                "Turnover must wait until the left arm clears the cup")
        require(groups["left_park"] == groups["right_place"] > groups["right_retreat"],
                "Left parking must follow turnover and overlap placement")
        clearance = next(job for job in reference_plan if job["name"] == "left_clear")
        require(clearance["body"]["speed"] == 100 and
                clearance["body"]["points"][0]["speed"] == 100,
                "Left cup clearance must retain the slow pick/lift speed")
        for arm in ("left", "right"):
            require([step["name"] for step in queue_steps if step["body"]["arm"] == arm] ==
                    [job["name"] for job in reference_plan if job["body"]["arm"] == arm],
                    "Queue changed same-arm order")
        jobs = session.execute_batch(capture_id=initial)
        client.check_lease()
        require(gateway.owner["client_id"] == "tertiary", "Lease was dropped between cups or captures")
        require(gateway.calls.count(("POST", "/control/acquire")) == 1, "Unexpected reacquisition between cups")
        require(("POST", "/control/release") not in gateway.calls, "Lease released before batch exit")
    finally:
        client.end_standby()
    require(gateway.owner is None, "Standby did not release control on exit")
    require(len(captures) == 5 and waits == [1], "Wrong capture/full-grid wait count")
    require(len(gateway.submissions) == 5, "Expected one queue per cup and separately verified Home jobs")
    require([len(queue) for queue in gateway.submissions] == [len(reference_plan)] * 3 + [1, 1],
            "Cup was split across multiple queue submissions")
    require([job["name"] for job in jobs if job["endpoint"] == RobotClient.HOME_ENDPOINT] ==
            ["workspace_right_home", "workspace_left_home"], "Expected exactly one final Home per arm")
    require(session.batch_progress["state"] == "completed" and
            session.batch_progress["completion_reason"] == "workspace_empty" and
            session.batch_progress["home_cycles"] == 1, "Finite batch did not end after empty-workspace Home")
    placed = [cup for cup in session.inventory["cups"] if cup.get("status") == "placed"]
    require(len(placed) == 3 and [cup["x"] for cup in placed] == [450, 560, 650],
            "Moved/new cups did not use the refreshed coordinates")
    require([cup["execution"]["placement"]["id"] for cup in placed] == [1, 1, 1],
            "Camera-cleared grid cell was not reused")
    require(not grid.snapshot()["locations"][0]["occupied"], "Final camera did not release the removed grid cup")
    require(not session.motion_uncertain and not recovery.required(), "Completed batch retained false recovery")
    return {"cup_count": 3, "captures": len(captures), "stationary_grid_waits": len(waits),
            "queue_submissions": len(gateway.submissions), "cup_queue_steps": len(reference_plan),
            "placement_ids": [1, 1, 1], "home_cycles": 1,
            "plan_sha256": hashlib.sha256(json.dumps(reference_plan, sort_keys=True).encode()).hexdigest()}


def verify_initial_grid_only(root, gateway):
    from cup_commands import CupSession
    from cup_inventory import atomic_json
    from handover_plan import right_via_pose
    from placing_grid import PlacingGrid
    from queued_robot_client import QueueRobotClient
    from recovery_journal import RecoveryJournal
    grid_path = root / "grid-only.json"
    grid_path.write_text(json.dumps({"cell_size_mm": 90, "locations": [
        {"id": 1, "x": 420, "y": -200, "z": 230, "occupied": True}]}))
    grid, captures = PlacingGrid(grid_path), []
    def capture(purpose):
        require(not captures and purpose == "scan", "Grid-only finite run started idle camera monitoring")
        captures.append(purpose)
        inventory = scene(grid, 100, [(100, 420)], True)
        cup = inventory["cups"][0]
        cup.update(y=-200, selectable=False, status="blocked", placement_location_ids=[1],
                   blocked_reason="Cup is already in the placement area.")
        inventory["source_presence"] = {"cup_detections": 0}
        return inventory, 100.0
    gateway.client_id, gateway.priority = "tertiary", 100
    client = QueueRobotClient(API, {})
    session = CupSession(capture, lambda value: atomic_json(root / "grid-only-cups.json", value), client,
        "offline-calibration", placing_grid=grid, recovery=RecoveryJournal(root / "grid-only-recovery.json"),
        between_cup_check=True, between_cup_grid_check=True, continuous_batch=False,
        right_park_pose=right_via_pose())
    before = len(gateway.submissions)
    require(client.try_begin_standby(), "Grid-only takeover could not acquire its mock lease")
    try:
        session.scan()
        session.execute_observed_batch(capture_id=session.inventory["capture"])
    finally:
        client.end_standby()
    require(len(captures) == 1, "Empty workspace was continuously rescanned")
    require(session.batch_progress["completion_reason"] == "workspace_empty" and
            session.batch_progress["home_cycles"] == 1, "Grid cups were mistaken for source cups")
    require(grid.snapshot()["locations"][0]["occupied"], "Home changed occupied grid state")
    require([len(queue) for queue in gateway.submissions[before:]] == [1, 1] and
            all(step["type"] == "home" for queue in gateway.submissions[before:] for step in queue),
            "Grid-only takeover sent a picking command or failed Home verification separation")
    return {"captures": 1, "source_cups": 0, "grid_occupied": True, "home_cycles": 1,
            "queue_submissions": len(gateway.submissions) - before}


def verify_queue_failures(gateway):
    import requests
    from handover_plan import handover_jobs
    from queued_robot_client import QueueRobotClient
    from robot_client import RobotCommandError
    plan = handover_jobs({"x": 500, "y": 100, "z": 50, "diameter": 50, "rz": 180},
                        place={"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False})
    client = QueueRobotClient(API, {})
    before = len(gateway.submissions)
    gateway.submission_error = requests.Timeout("Offline lost submission response")
    try:
        client.execute_jobs(plan)
    except RobotCommandError:
        pass
    else:
        raise AssertionError("Ambiguous queue submission was accepted")
    finally:
        gateway.submission_error = None
    require(len(gateway.submissions) == before + 1, "Ambiguous motion submission was retried")
    bad = copy.deepcopy(gateway.queue)
    bad["steps"][0]["state"] = "running"
    try:
        client._validate_queue_status(bad)
    except RobotCommandError:
        pass
    else:
        raise AssertionError("Incomplete queue was accepted as done")
    gateway.client_id, gateway.priority = "secondary", 200
    before = len(gateway.submissions)
    try:
        client.try_begin_standby("tertiary", 100)
    except RobotCommandError:
        pass
    else:
        raise AssertionError("Wrong-tier credential was accepted")
    require(len(gateway.submissions) == before and gateway.owner is None,
            "Wrong-tier lease was retained or used for movement")
    return {"ambiguous_submit_retried": False, "incomplete_done_rejected": True,
            "wrong_tier_rejected": True}


def verify(app_root):
    app_root = Path(app_root).resolve(strict=True)
    sys.path[:0] = [str(app_root), str(app_root / "capture_and_detect")]
    guard, attempts = ImportGuard(), []
    sys.meta_path.insert(0, guard)
    def blocked(*_args, **_kwargs):
        attempts.append("blocked")
        raise AssertionError("Real network access is forbidden during offline verification")
    try:
        with ExitStack() as stack:
            for name in ("connect", "connect_ex"):
                stack.enter_context(patch.object(socket.socket, name, blocked))
            stack.enter_context(patch.object(socket, "create_connection", blocked))
            stack.enter_context(patch.object(socket, "getaddrinfo", blocked))
            stack.enter_context(patch.object(subprocess, "Popen", blocked))
            import requests
            stack.enter_context(patch.object(requests.sessions.Session, "request", blocked))
            modules = ("lynx.capture", "lynx.runtime", "lynx.runtime_lock", "lynx.picker",
                       "lynx.standby", "queued_robot_client", "workspace_check", "device_service")
            for module in modules:
                importlib.import_module(module)
            require(not HEAVY_MODULES.intersection(sys.modules), "Heavy dependencies were loaded")
            from robot_client import RobotClient
            gateway = FakeGateway(RobotClient)
            stack.enter_context(patch.object(requests, "get", gateway.get))
            stack.enter_context(patch.object(requests, "post", gateway.post))
            stack.enter_context(patch.object(requests, "delete", blocked))
            with tempfile.TemporaryDirectory(prefix="lynx-offline-queue-") as directory:
                with redirect_stdout(io.StringIO()):
                    batch = verify_finite_queue_batch(Path(directory), gateway)
                    grid_only = verify_initial_grid_only(Path(directory), gateway)
                    failures = verify_queue_failures(gateway)
            require(not attempts, "Verifier attempted real network access")
            require(all(response.closed for response in gateway.responses), "HTTP responses were not closed")
            return {"ok": True, "real_network_calls": 0, "model_workers_started": 0,
                    "persistent_state_used": False, "heavy_modules_loaded": [],
                    "imports": list(modules), "finite_batch": batch, "initial_grid_only": grid_only,
                    "failure_checks": failures}
    finally:
        sys.meta_path.remove(guard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path("/app") if Path("/app/capture_and_detect").is_dir() else Path(__file__).resolve().parents[2]
    parser.add_argument("--app-root", type=Path, default=default)
    args = parser.parse_args()
    print(json.dumps(verify(args.app_root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
