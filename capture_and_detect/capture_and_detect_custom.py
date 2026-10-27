"""Scan cups, select one by ID, and pick, hand over, place and home over RWS.

Enter / scan saves observations without movement. goto <id> verifies that cup,
then runs the existing left-to-right handover and returns both arms home.
hover <id> only moves the left arm above a cup. --dry-run previews the requests
without acquiring control or moving hardware.
See README.md in this directory for commands, calibration details and limitations.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from camera_client import CameraClient, CameraError
from cup_commands import CupSession
from cup_inventory import (SelectionError, atomic_json, build_inventory, normalize_offset,
                           save_inventory)
from robot_client import RobotClient, RobotCommandError
from handover_plan import pick_position_problem
from recovery_journal import RecoveryJournal
from language_commands import LanguageController
from local_llm import OllamaIntentClient

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE.parent / "calibration" / "calibration.json"
KEEP_CLASSES = ["CUP", "CUP BASE", "CUP HANDLE"]
CUP_Z_MIN, CUP_Z_MAX = 10, 150
CSV_FIELDS = ["capture", "class", "confidence", "x_mm", "y_mm", "x_px", "y_px",
              "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm", "mask_area_px"]
HELP = """Commands:
  Enter / scan        Detect and save cups; no robot movement.
  list                Show cups, saved XYZ, and the corrected hover target.
  show 2              Show all stored information for cup 2.
  goto 2              Verify cup 2; pick with left, hand to right, place it,
                      then finish with both arms home.
  hover 2             Verify cup 2; move the left arm above it without gripping.
  reset               Acknowledge checking the robot after an unconfirmed move.
                      Sends nothing. A new scan is still required.
  help                Show this help.
  q / quit            Exit (does not stop an already dispatched movement).
IDs belong to one scan and may change after scanning again.
"""
LANGUAGE_HELP = """Local language mode:
  Type a sentence, for example: pick cup two / hover above the rightmost cup.
  A movement proposal shows a cup ID and a one-use confirmation code.
  confirm <code>      Execute that exact proposal through the existing checks.
  cancel             Discard a pending proposal; does not stop a moving robot.
  Exact terminal commands (scan, goto 2, hover 2, reset, etc.) still work directly.
Left/right refer to the numbered camera image. Colors and gestures are not known.
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
        """Mask centre (u, v), median depth inside the mask -> camera and robot mm."""
        ys, xs = np.nonzero(mask)
        u, v = float(xs.mean()), float(ys.mean())
        vals = depth[mask]
        vals = vals[vals > 0]                      # 0 = no depth there
        z = float(np.median(vals)) if vals.size else None

        x = y = None
        robot = (None, None, None)
        if z:                                      # pinhole model
            x = (u - self.K["cx"]) * z / self.K["fx"]
            y = (v - self.K["cy"]) * z / self.K["fy"]
            robot = self.calibration.to_robot(x, y, z)
        return {
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
        }


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

    def save(self, inventory):
        save_inventory(self.folder, inventory)

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
    def __init__(self, camera, detector, store, weights):
        self.camera, self.detector, self.store = camera, detector, store
        self.weights = Path(weights)

    def capture(self, purpose):
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
        objects, annotated = self.detector.analyse(rgb, depth)
        elapsed_ms = (time.perf_counter() - before) * 1000
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + "_" + purpose
        images = {kind: str(self.store.folder / f"{kind}_{stamp}.{extension}")
                  for kind, extension in (("rgb", "jpg"), ("depth", "png"), ("annotated", "jpg"))}
        inventory = build_inventory(objects, capture_id=stamp, frame=frame,
                                    calibration_id=calibration.digest, tag_offset=calibration.offset,
                                    gateway=self.camera.api, images=images,
                                    model={"weights": str(self.weights), "confidence_threshold": self.detector.conf,
                                           "inference_ms": round(elapsed_ms, 1)})
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
                log("Robot reported the pick, handover and placement complete; both home poses checked. Scan again before another cup.")
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
    if language is None:
        return handle_terminal(session, line)
    words = line.strip().split()
    first = words[0].lower() if words else ""
    if first == "confirm":
        if len(words) != 2:
            raise SelectionError("Type confirm followed by the displayed proposal code.")
        result = language.confirm(words[1])
    elif first in ("cancel", "stop"):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(HERE / "Data" / "Detections"))
    parser.add_argument("--calibration", default=str(CALIBRATION_FILE))
    parser.add_argument("--gateway", default="http://169.254.125.202:8000")
    parser.add_argument("--conf", type=float, default=0.5)
    parser.add_argument("--dry-run", action="store_true", help="Preview the entire RWS sequence; no control acquisition or robot commands")
    parser.add_argument("--speed", type=positive, default=50.0, help="Requested speed for terminal moves, mm/s (1..1500; default 50)")
    parser.add_argument("--max-result-age", type=positive, default=15.0, help="Maximum verification image age before approach, descent and grip, seconds (default 15)")
    parser.add_argument("--match-mm", type=positive, default=20.0, help="Maximum 3D displacement on verification, mm (default 20)")
    parser.add_argument("--match-px", type=positive, default=40.0, help="Maximum pixel displacement on verification (default 40)")
    parser.add_argument("--llm", action="store_true", help="Enable local Ollama language commands with movement previews and confirmation")
    parser.add_argument("--llm-model", default="qwen3:4b", help="Installed local Ollama model (default qwen3:4b)")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Local Ollama address; loopback hosts only")
    parser.add_argument("--check-llm", action="store_true", help="Test local interpretation only; no camera, token, YOLO or robot connection")
    args = parser.parse_args(argv)
    if not math.isfinite(args.conf) or not 0 < args.conf <= 1:
        parser.error("--conf must be greater than 0 and at most 1")
    if not 1 <= args.speed <= 1500:
        parser.error("--speed must be between 1 and 1500 mm/s")
    interpreter = None
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
    try:
        headers = {"Authorization": f"Bearer {load_token()}"}
        api = args.gateway.rstrip("/") + "/api/v1"
        camera = CameraClient(api, headers)
        calibration = Calibration(args.calibration)
        model, weights = load_model()
        detector = Detector(model, calibration, camera.intrinsics(), args.conf)
        store = CaptureStore(args.output)
        captures = CaptureService(camera, detector, store, weights)
        recovery = RecoveryJournal(store.folder / "robot_recovery.json", legacy_inventory_path=store.folder / "cups.json")
        session = CupSession(captures.capture, store.save, RobotClient(api, headers, dry_run=args.dry_run),
                             calibration.digest, tag_offset=calibration.offset, speed=args.speed,
                             max_result_age=args.max_result_age, match_mm=args.match_mm, match_px=args.match_px,
                             recovery=recovery)
        language = LanguageController(session, interpreter, audit_dir=store.folder / "language") if args.llm else None
    except (OSError, ValueError, KeyError, CameraError) as error:
        log(f"Startup failed: {error}")
        return 1
    log(f"Ready. Saving to: {store.folder}")
    log(f"Gateway: {api}; terminal movement speed: {args.speed:g} mm/s")
    log("DRY RUN: all RWS moves, gripper and home requests will only be printed." if args.dry_run else
        "LIVE: goto picks, hands over, places and homes BOTH arms. hover only approaches; scan only saves detections.")
    log(HELP)
    if language is not None:
        log(f"LOCAL LLM: {args.llm_model} via {args.ollama_url}")
        log(LANGUAGE_HELP)
    if session.motion_uncertain:
        log("An earlier robot sequence is unfinished. Check both arms and the cup, then use reset and scan.")
    try:
        while True:
            try:
                if not handle_input(session, input("cups> ").strip(), language):
                    break
            except (SelectionError, CameraError, RobotCommandError, OSError, ValueError) as error:
                log(f"Blocked: {error}")
                if session.motion_uncertain:
                    log("The sequence was interrupted or a command was not confirmed. Check both arms and the cup before reset; no automatic retry or return home.")
    except (KeyboardInterrupt, EOFError):
        log("\nExiting. This does not cancel a movement already dispatched to the robot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
