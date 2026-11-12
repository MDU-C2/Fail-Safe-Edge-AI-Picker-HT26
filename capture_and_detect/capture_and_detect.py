"""Cup detection, placement grid, operator controls and browser dashboard.

Enter / scan saves observations without movement. goto <id> verifies that cup,
then runs the existing left-to-right handover and returns both arms home.
hover <id> only moves the left arm above a cup. --dry-run previews the requests
without acquiring control or moving hardware.
Use --web for the browser UI, or --web --demo for a hardware-free demonstration.
The terminal's --left-only mode picks, lifts and replaces a cup without a handover.
See README.md in this directory for commands, calibration details and limitations.
"""

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from camera_client import CameraClient, CameraError
from cup_commands import CupSession
from cup_base_depth import cup_base_depth
from detection_masks import encode_mask
from cup_inventory import (SelectionError, atomic_json, build_inventory, normalize_offset,
                           save_inventory)
from robot_client import RobotClient, RobotCommandError
from handover_plan import (CUP_Z_MIN, CUP_Z_MAX, DEFAULT_SPEED, HOME_SPEED,
                           FLIP_SPEED, pick_position_problem)
from placing_grid import PlacingGrid
from placement_vision import PlacementVision, SCAN_FRAMES
from recovery_journal import RecoveryJournal
from language_commands import LanguageController
from local_llm import DisabledIntentClient, OllamaIntentClient
from operator_commands import START_COMMANDS, STOP_COMMANDS
from terminal_controller import TerminalController

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE.parent / "calibration" / "calibration.json"
KEEP_CLASSES = ["CUP", "CUP BASE", "CUP HANDLE"]
CSV_FIELDS = ["capture", "class", "confidence", "x_mm", "y_mm", "x_px", "y_px",
              "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm", "mask_area_px"]
GRIP_FIELDS = ["capture", "cup", "diameter_mm", "grip_left_mm", "grip_right_mm", "status"]
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


def load_token():
    """GATEWAY_TOKEN from the environment, or (Windows) where setx saved it."""
    token = os.environ.get("GATEWAY_TOKEN")
    if not token and os.name == "nt":
        # setx saves the token for new windows only; read it straight from where setx stores it
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token = winreg.QueryValueEx(key, "GATEWAY_TOKEN")[0]
        except OSError:
            pass
    if not token:
        raise SystemExit('No gateway token. Set it first, e.g. in PowerShell:  $env:GATEWAY_TOKEN = "<token>"')
    return token


def load_model():
    """NCNN on the Lynx (the Lynx container sets PICKER_DEVICE=lynx), best.pt elsewhere."""
    lynx = os.environ.get("PICKER_DEVICE", "").lower() == "lynx"
    path = HERE / ("best_ncnn_model" if lynx else "best.pt")
    if not path.exists():
        raise SystemExit(f"Model not found: {path}\n"
                         f"Put best.pt and best_ncnn_model/ in the same folder as capture_and_detect.py.")
    log(f"Loading {path.name} ...")
    from ultralytics import YOLO
    return YOLO(str(path), task="segment"), path


class Calibration:
    """Camera mm -> saved robot mm; the movement planner applies the XY tag offset."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()
        cal = json.loads(raw)
        self.scale = cal["scale"]
        self.R, self.t = np.array(cal["R"], dtype=float), np.array(cal["t"], dtype=float)
        self.offset = normalize_offset(cal.get("tag_offset", (0, 0, 0)))
        if (self.R.shape != (3, 3) or self.t.shape != (3,) or not np.isfinite(self.R).all()
                or not np.isfinite(self.t).all() or not math.isfinite(self.scale) or self.scale <= 0):
            raise ValueError("Invalid calibration matrix, translation, or scale")

    def ensure_unchanged(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.digest:
            raise SelectionError("Calibration file changed. Restart and scan again.")

    def to_robot(self, x, y, z):
        return tuple(self.scale * self.R @ np.array([x, y, z]) + self.t)


class Detector:
    """YOLO segmentation + aligned depth -> one dict per object (pixels, camera mm, robot mm)."""

    def __init__(self, model, calibration, K, conf):
        self.model, self.calibration, self.K, self.conf = model, calibration, K, conf
        self.classes = [i for i, n in model.names.items() if n in KEEP_CLASSES]
        log("Detecting:", [model.names[i] for i in self.classes])

    def analyse(self, rgb, depth):
        """Run YOLO on one frame; return (list of objects, annotated image)."""
        result = self.model.predict(rgb, conf=self.conf, classes=self.classes,
                                    retina_masks=True, verbose=False)[0]
        objects, keep, boxes = [], [], []
        masks = [] if result.masks is None else result.masks.data
        for j, (box, mask) in enumerate(zip(result.boxes, masks)):
            mask = mask.cpu().numpy().astype(bool)
            if mask.any():
                keep.append(j)
                boxes.append(box.xyxy[0].tolist())
                obj = self.describe(self.model.names[int(box.cls)], float(box.conf), mask, depth)
                obj["detection_id"] = j
                obj["bbox_xyxy"] = [round(float(value), 1) for value in box.xyxy[0].tolist()]
                objects.append(obj)
        mark_cup_bases(objects, boxes)

        annotated = result[keep].plot() if keep else rgb.copy()
        for o in objects:                          # mark the point we report
            cv2.drawMarker(annotated, (int(o["x_px"]), int(o["y_px"])), (0, 0, 255),
                           cv2.MARKER_CROSS, 25, 3)
        return objects, annotated

    def describe(self, name, confidence, mask, depth):
        """Mask centre + depth -> coordinates; CUP BASE requires reliable core depth."""
        ys, xs = np.nonzero(mask)
        u, v = float(xs.mean()), float(ys.mean())
        quality = None
        if name == "CUP BASE":
            z, quality = cup_base_depth(mask, depth)
        else:
            vals = depth[mask]
            vals = vals[vals > 0]                  # 0 = no depth there
            z = float(np.median(vals)) if vals.size else None

        x = y = None
        robot = (None, None, None)
        if z:                                      # pinhole model
            x = (u - self.K["cx"]) * z / self.K["fx"]
            y = (v - self.K["cy"]) * z / self.K["fy"]
            robot = self.calibration.to_robot(x, y, z)
        diameter_px = 2 * math.sqrt(int(mask.sum()) / math.pi)
        diameter = diameter_px * z / self.K["fx"] if z else None
        result = {
            "class": name,
            "confidence": round(confidence, 3),
            "x_mm": mm(x),
            "y_mm": mm(y),
            "x_px": round(u, 1),
            "y_px": round(v, 1),
            "z_mm": mm(z),                         # distance from the camera
            "robot_x_mm": mm(robot[0]),
            "robot_y_mm": mm(robot[1]),
            "robot_z_mm": mm(robot[2]),
            "mask_area_px": int(mask.sum()),
            "segmentation": encode_mask(mask),
            "diameter_mm": mm(diameter),
        }
        if quality is not None:
            result["depth_quality"] = quality
        return result


def mm(value):
    """Round to 0.1 mm; None (no depth) stays None."""
    return None if value is None else round(float(value), 1)


def mark_cup_bases(objects, boxes):
    """Set "is_cup" on every CUP BASE. It is a cup if one of these is true (the robot's joint
    was once detected as CUP BASE, with no CUP around it and high above the table):
        in_cup       its centre lies inside a detected CUP
        near_handle  a CUP HANDLE is next to it (within half a base size)
        cup_height   its robot Z is that of a cup on the table (CUP_Z_MIN..CUP_Z_MAX); straight
                     from above, an upside-down mug often gives only a CUP BASE and no CUP"""
    cup_boxes = [b for o, b in zip(objects, boxes) if o["class"] == "CUP"]
    handle_boxes = [b for o, b in zip(objects, boxes) if o["class"] == "CUP HANDLE"]
    for o, (bx1, by1, bx2, by2) in zip(objects, boxes):
        if o["class"] != "CUP BASE":
            continue
        o["in_cup"] = any(x1 <= o["x_px"] <= x2 and y1 <= o["y_px"] <= y2
                          for x1, y1, x2, y2 in cup_boxes)
        mx, my = (bx2 - bx1) / 2, (by2 - by1) / 2
        o["near_handle"] = any(x1 <= bx2 + mx and x2 >= bx1 - mx and y1 <= by2 + my and y2 >= by1 - my
                               for x1, y1, x2, y2 in handle_boxes)
        o["cup_height"] = o["robot_z_mm"] is not None and CUP_Z_MIN <= o["robot_z_mm"] <= CUP_Z_MAX
        o["is_cup"] = o["in_cup"] or o["near_handle"] or o["cup_height"]


class CaptureStore:
    """Save raw detections and schema-v2 cup observations without moving the robot."""

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._grip_records = set()

    def save(self, inventory):
        save_inventory(self.folder, inventory)
        for cup in inventory.get("cups", []):
            verification = cup.get("last_verification", {})
            execution = cup.get("execution", {})
            if (cup.get("status") not in {"placed", "replaced", "movement_unconfirmed"}
                    or execution.get("action") != "goto"):
                continue
            key = (inventory["capture"], cup["id"], verification.get("capture"), cup["status"])
            if key in self._grip_records:
                continue
            record = {"capture": inventory["capture"], "cup": cup["id"],
                      "diameter_mm": verification.get("grip_geometry", {}).get("diameter"),
                      "grip_left_mm": cup.get("grip_left_mm"),
                      "grip_right_mm": cup.get("grip_right_mm"), "status": cup["status"]}
            try:
                path = self.folder / "grips.csv"
                new_file = not path.exists() or path.stat().st_size == 0
                with path.open("a", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=GRIP_FIELDS)
                    if new_file:
                        writer.writeheader()
                    writer.writerow(record)
                self._grip_records.add(key)
            except OSError as error:
                # Motion and recovery records above remain authoritative. A
                # telemetry export must never retry an already completed move.
                log(f"Could not append optional gripper telemetry: {error}")

    def save_capture(self, inventory, rgb, depth, annotated, objects, K, purpose):
        stamp = inventory["capture"]
        for kind, array in (("rgb", rgb), ("depth", depth), ("annotated", annotated)):
            if not cv2.imwrite(inventory["images"][kind], array):
                raise OSError(f"Could not save {inventory['images'][kind]}")
        record = {"capture": stamp, "units": "mm", "purpose": purpose, "intrinsics": K,
                  "frame": inventory["frame"], "calibration_sha256": inventory["calibration_sha256"],
                  "tag_offset_mm": inventory["tag_offset_mm"], "objects": objects}
        atomic_json(self.folder / f"detections_{stamp}.json", record)
        if purpose == "scan":
            atomic_json(self.folder / "latest.json", record)
        csv_path = self.folder / "detections.csv"
        if csv_path.exists():
            with csv_path.open() as stream:
                if stream.readline().strip() != ",".join(CSV_FIELDS):
                    csv_path.rename(self.folder / f"detections_old_{stamp}.csv")
        new_file = not csv_path.exists()
        with csv_path.open("a", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerows({"capture": stamp, **obj} for obj in objects)
        save_inventory(self.folder, inventory, latest=purpose == "scan")


class CaptureService:
    def __init__(self, camera, detector, store, weights, *, placing_grid=None,
                 grid_table_z=0.0, grid_cell_size=80.0, table_reference_path=None):
        self.camera, self.detector, self.store = camera, detector, store
        self.weights = Path(weights)
        self.placing_grid = placing_grid
        self._default_grid_cell_size = grid_cell_size
        self.table_reference_path = (Path(table_reference_path) if table_reference_path is not None
                                     else placing_grid.path.with_suffix(".table-reference.json")
                                     if placing_grid is not None else None)
        self._table_reference_bytes = self._read_table_reference()
        if table_reference_path is not None and self._table_reference_bytes is None:
            raise SelectionError("The requested empty-table reference file is missing.")
        try:
            self._table_reference_value = (json.loads(self._table_reference_bytes)
                                           if self._table_reference_bytes is not None else None)
        except (ValueError, UnicodeError) as error:
            raise SelectionError("Empty-table reference contains invalid JSON; rebuild it from a verified empty scan.") from error
        if self._table_reference_bytes is not None and not isinstance(self._table_reference_value, dict):
            raise SelectionError("The empty-table reference must be a JSON object.")
        self.placement_vision = (PlacementVision(detector.calibration, detector.K,
                                table_z=grid_table_z, cell_size=grid_cell_size,
                                table_reference=self._table_reference_value)
                                 if placing_grid is not None else None)

    def _read_table_reference(self):
        if self.table_reference_path is None:
            return None
        try:
            with self.table_reference_path.open("rb") as stream:
                value = stream.read(65537)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SelectionError("Cannot read the empty-table reference file.") from error
        if len(value) > 65536:
            raise SelectionError("Empty-table reference exceeds its size limit.")
        return value

    def _check_table_reference(self):
        if self._read_table_reference() != self._table_reference_bytes:
            raise SelectionError("Empty-table reference changed. Restart and scan again.")

    def _observe(self):
        calibration = self.detector.calibration
        calibration.ensure_unchanged()
        if self.camera.intrinsics() != self.detector.K:
            raise CameraError("Camera intrinsics changed. Restart and scan again.")
        rgb, depth, frame = self.camera.capture()
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[:2] != (self.detector.K.get("height"), self.detector.K.get("width")):
            raise CameraError("Image dimensions do not match the camera intrinsics.")
        if depth.shape != rgb.shape[:2]:
            raise CameraError("Depth dimensions do not match the aligned RGB image.")
        started = frame["observation_started_monotonic"]
        before = time.perf_counter()
        analyse_observation = getattr(self.detector, "analyse_observation", None)
        if callable(analyse_observation):
            objects, annotated = analyse_observation(rgb, depth, frame)
        else:
            objects, annotated = self.detector.analyse(rgb, depth)
        elapsed_ms = (time.perf_counter() - before) * 1000
        return rgb, depth, frame, started, objects, annotated, elapsed_ms

    def capture(self, purpose):
        from inference_integration import observation_group
        # A recovered Mac cannot replace the selected provider halfway through
        # one multi-frame grid assessment. Failover restarts its evidence set.
        with observation_group(self.detector):
            return self._capture_group(purpose)

    def _capture_group(self, purpose):
        calibration = self.detector.calibration
        self._check_table_reference()
        snapshot, grid_error = None, None
        if self.placing_grid is not None:
            try:
                snapshot = self.placing_grid.snapshot()
                cell_size = snapshot.get("cell_size_mm", self._default_grid_cell_size)
                if self.placement_vision.cell_size != cell_size:
                    self.placement_vision = PlacementVision(
                        calibration, self.detector.K, table_z=self.placement_vision.table_z,
                        cell_size=cell_size, table_reference=self._table_reference_value)
            except SelectionError as error:
                grid_error = str(error)
        observations, sequences, inference_results = [], [], []
        count = SCAN_FRAMES if purpose == "scan" and snapshot is not None else 1
        completed, restarts = 0, 0
        generation = getattr(self.detector, "generation", None)
        while completed < count:
            rgb, depth, frame, started, objects, annotated, elapsed_ms = self._observe()
            current_generation = getattr(self.detector, "generation", None)
            if current_generation != generation:
                if completed:
                    restarts += 1
                    if restarts > 1:
                        raise CameraError("Inference changed repeatedly during this scan; scan again")
                    observations, sequences, inference_results = [], [], []
                    completed = 0
                generation = current_generation
            metadata = getattr(self.detector, "inference_metadata", None)
            if metadata is not None:
                inference_results.append(dict(metadata))
            if snapshot is not None:
                observations.append(self.placement_vision.assess(depth, objects, snapshot["locations"]))
                sequences.append(frame.get("sequence"))
            completed += 1
            if completed < count:
                # Only the final observation is saved. Release previous image
                # buffers before _observe() allocates the next RGB/depth/masks;
                # tuple assignment otherwise retains them until that call ends.
                del rgb, depth, annotated
        calibration.ensure_unchanged()
        self._check_table_reference()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + "_" + purpose
        images = {kind: str(self.store.folder / f"{kind}_{stamp}.{extension}")
                  for kind, extension in (("rgb", "jpg"), ("depth", "png"), ("annotated", "jpg"))}
        inventory = build_inventory(objects, capture_id=stamp, frame=frame,
                                    calibration_id=calibration.digest, tag_offset=calibration.offset,
                                    gateway=self.camera.api, images=images,
                                    model={"weights": str(self.weights), "confidence_threshold": self.detector.conf,
                                           "inference_ms": round(elapsed_ms, 1)})
        if inference_results:
            inventory["model"]["inference"] = inference_results[-1]
            inventory["model"]["observation_results"] = inference_results
        if observations:
            self.placement_vision.exclude_placed_cups(inventory["cups"], snapshot["locations"])
            if purpose == "scan":
                observation = self.placement_vision.combine(snapshot, observations, sequences)
            else:
                observation = {"frame_count": 1, "slots": observations[-1], "applied": False,
                               "released_ids": [], "message": "Verification image only; placement status unchanged."}
            inventory["placement_observation"] = observation
            observation["cell_size_mm"] = self.placement_vision.cell_size
            if self.placement_vision.table_reference_metadata is not None:
                observation["table_reference"] = copy.deepcopy(self.placement_vision.table_reference_metadata)
                observation["table_reference"]["file_sha256"] = hashlib.sha256(self._table_reference_bytes).hexdigest()
            self.placement_vision.draw(annotated, observation["slots"])
        elif grid_error:
            inventory["placement_observation"] = {"frame_count": 0, "slots": [], "applied": False,
                "released_ids": [], "message": "Placement camera check unavailable: " + grid_error}
        for cup in inventory["cups"]:
            u, v = cup["pixel"]["u"], cup["pixel"]["v"]
            if u is None or v is None:
                continue
            label = f"Cup {cup['id']}" + (" BLOCKED" if not cup["selectable"] else "")
            if cup["selectable"] and pick_position_problem(cup, calibration.offset):
                label += " HOVER ONLY"
            position = (max(0, min(int(u), annotated.shape[1] - 200)), max(25, int(v) - 15))
            cv2.putText(annotated, label, position, cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 5)
            cv2.putText(annotated, label, position, cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 255, 0) if cup["selectable"] else (0, 180, 255), 2)
        self.store.save_capture(inventory, rgb, depth, annotated, objects, self.detector.K, purpose)
        log(f"[{purpose}] {len(inventory['cups'])} cup candidate(s), YOLO {elapsed_ms:.0f} ms")
        return inventory, started


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
    log(f"Numbered image: {inventory['images']['annotated']}")
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--web" in argv:
        from web_app import main as web_main
        return web_main([argument for argument in argv if argument != "--web"])
    from inference_integration import (add_inference_arguments, create_detector, create_robot,
                                       validate_inference_arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", action="store_true", help="Open the browser workflow; use --web --help for its options")
    parser.add_argument("--left-only", action="store_true", help="Pick, lift and replace each selected cup with the left arm; no handover or placement-grid update")
    parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
    parser.add_argument("--calibration", default=str(CALIBRATION_FILE))
    parser.add_argument("--placing-grid", default=str(HERE / "placing_grid.json"), help="Placement locations and their occupied/reserved flags")
    parser.add_argument("--grid-table-z", type=float, default=0.0, help="Physical robot Z of the table, mm (default 0)")
    parser.add_argument("--grid-cell-size", type=float, default=80.0, help="Fallback cell width in mm (default 80; 20..200); the grid file's cell_size_mm takes precedence")
    parser.add_argument("--gateway", default="http://169.254.125.202:8000")
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true", help="Preview the entire RWS sequence; no control acquisition or robot commands")
    parser.add_argument("--speed", type=positive, default=None,
                        help=f"Override motion speed, including home (1..1500 mm/s). Without this option: "
                             f"normal {DEFAULT_SPEED}, automatic home {HOME_SPEED}. Turning is capped at 100.")
    parser.add_argument("--max-result-age", type=positive, default=15.0, help="Maximum verification image age before approach, descent and grip, seconds (default 15)")
    parser.add_argument("--match-mm", type=positive, default=20.0, help="Maximum 3D displacement on verification, mm (default 20)")
    parser.add_argument("--match-px", type=positive, default=40.0, help="Maximum pixel displacement on verification (default 40)")
    parser.add_argument("--llm", action="store_true", help="Enable local Ollama commands and saved-cup visual matching with movement confirmation")
    parser.add_argument("--llm-model", default="qwen3-vl:4b-instruct", help="Installed local Ollama model (default qwen3-vl:4b-instruct)")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Local Ollama address; loopback hosts only")
    parser.add_argument("--check-llm", action="store_true", help="Test local interpretation only; no camera, token, YOLO or robot connection")
    add_inference_arguments(parser)
    args = parser.parse_args(argv)
    validate_inference_arguments(args, parser)
    if not math.isfinite(args.conf) or not 0 < args.conf <= 1:
        parser.error("--conf must be greater than 0 and at most 1")
    if args.speed is not None and not 1 <= args.speed <= 1500:
        parser.error("--speed must be between 1 and 1500 mm/s")
    if not math.isfinite(args.grid_table_z) or not math.isfinite(args.grid_cell_size) or not 20 <= args.grid_cell_size <= 200:
        parser.error("--grid-table-z must be finite; --grid-cell-size must be 20..200 mm")
    interpreter = DisabledIntentClient()
    if args.llm or args.check_llm:
        try:
            interpreter = OllamaIntentClient(model=args.llm_model, url=args.ollama_url)
            if args.check_llm:
                scene = {"capture_id": "model-check-only", "scan_usable": True,
                         "recovery_required": False,
                         "direction_reference": "left/right in the numbered camera image",
                         "known_attributes": ["id", "image_position", "availability"],
                         "cups": [{"id": 2, "image_x": 100, "image_y": 100,
                                   "hover_available": True, "pick_available": True,
                                   "blocked_reason": None, "status": "available"}]}
                intent = interpreter.interpret("Hover above cup two", scene)
                log(json.dumps(intent, indent=2))
                if intent["action"] != "hover" or intent["selector"] != "id" or intent["cup_id"] != 2:
                    log("Model check failed: the sample request was not interpreted as hover cup 2. No robot connection was made.")
                    return 1
                log("Local model check passed. No camera or robot connection was made.")
                return 0
        except ValueError as error:
            log(f"Local model setup failed: {error}")
            return 1
    detector = None
    try:
        headers = {"Authorization": f"Bearer {load_token()}"}
        api = args.gateway.rstrip("/") + "/api/v1"
        camera = CameraClient(api, headers)
        calibration = Calibration(args.calibration)
        detector, weights = create_detector(args, calibration, camera.intrinsics())
        store = CaptureStore(args.output)
        grid = None if args.left_only else PlacingGrid(args.placing_grid)
        captures = CaptureService(camera, detector, store, weights, placing_grid=grid,
                                  grid_table_z=args.grid_table_z, grid_cell_size=args.grid_cell_size)
        recovery = RecoveryJournal(store.folder / "robot_recovery.json", legacy_inventory_path=store.folder / "cups.json")
        session = CupSession(captures.capture, store.save, create_robot(args, api, headers),
                             calibration.digest, tag_offset=calibration.offset, speed=args.speed,
                             max_result_age=args.max_result_age, match_mm=args.match_mm, match_px=args.match_px,
                             recovery=recovery, placing_grid=grid, left_only=args.left_only)
        from named_cups import NamedCupStore
        from cup_visual_matcher import OllamaCupMatcher
        library = NamedCupStore(store.folder / "saved_cups.json", image_root=store.folder)
        matcher = OllamaCupMatcher(model=args.llm_model, url=args.ollama_url) if args.llm else None
        language = LanguageController(session, interpreter, audit_dir=store.folder / "language",
                                      named_cups=library, cup_matcher=matcher)
    except (OSError, ValueError, KeyError, CameraError, RobotCommandError) as error:
        if detector is not None and callable(getattr(detector, "close", None)):
            detector.close()
        log(f"Startup failed: {error}")
        return 1
    log(f"Ready. Saving to: {store.folder}")
    log(f"Gateway: {api}; terminal movement speed: {session.speed:g} mm/s; automatic home: {session.home_speed:g} mm/s")
    log("LEFT ONLY: picks return to their original position; the right arm and placement grid are not used."
        if args.left_only else
        f"Placement grid: {args.placing_grid}; cup turning: {min(session.speed, FLIP_SPEED):g} mm/s, 30% acceleration")
    log("DRY RUN: all RWS moves, gripper and home requests will only be printed." if args.dry_run else
        ("LIVE: goto picks, lifts, replaces and homes the LEFT arm. hover only approaches; scan only saves detections."
         if args.left_only else
         "LIVE: goto picks, hands over, places and homes BOTH arms. hover only approaches; scan only saves detections."))
    log(HELP)
    if args.llm:
        log(f"LOCAL LLM: {args.llm_model} via {args.ollama_url}")
        log(LANGUAGE_HELP)
    if session.motion_uncertain:
        log("An earlier robot sequence is unfinished. Check both arms and the cup, then use reset and scan.")
    terminal = TerminalController(session, language,
                                  lambda line: handle_input(session, line, language), output=log)
    interrupted = False
    try:
        while True:
            if not terminal.submit(input("cups> ").strip()):
                break
    except (KeyboardInterrupt, EOFError):
        interrupted = True
        log("\nExiting; an active command will receive a software stop request. No automatic home or resume.")
    finally:
        terminal.close(interrupt=interrupted)
        if callable(getattr(detector, "close", None)):
            detector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
