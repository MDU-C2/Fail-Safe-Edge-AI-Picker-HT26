"""Camera-only capture and audit pipeline for the command-line Lynx picker."""
import copy
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from camera_client import CameraError
from cup_inventory import (SelectionError, atomic_json, build_inventory,
                           normalize_offset, save_inventory)
from table_reference_contract import validate_reference, _identity
from .placement_records import combine, exclude_placed_cups
from .raw_images import write_png


CSV_FIELDS = ["capture", "class", "confidence", "x_mm", "y_mm", "x_px", "y_px",
              "z_mm", "robot_x_mm", "robot_y_mm", "robot_z_mm", "mask_area_px", "robot_z_frames_mm"]
GRIP_FIELDS = ["capture", "cup", "diameter_mm", "grip_left_mm", "grip_right_mm", "status"]


def log(*parts):
    print(*parts, flush=True)


def bounded_read(path, limit=65536):
    with Path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise SelectionError(f"Configuration exceeds its size limit: {Path(path).name}")
    return value


class Calibration:
    def __init__(self, path):
        self.path = Path(path)
        self.raw = bounded_read(path)
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.value = json.loads(self.raw)
        self.scale, self.R, self.t = (self.value[key] for key in ("scale", "R", "t"))
        self.offset = normalize_offset(self.value.get("tag_offset", (0, 0, 0)))

    def ensure_unchanged(self):
        if bounded_read(self.path) != self.raw:
            raise SelectionError("Calibration file changed. Restart and scan again.")


class CaptureStore:
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


    def save_capture(self, inventory, rgb, depth, objects, intrinsics, purpose):
        width, height = intrinsics["width"], intrinsics["height"]
        write_png(inventory["images"]["rgb"], rgb, width, height)
        write_png(inventory["images"]["depth"], depth, width, height, depth=True)
        record = {"capture": inventory["capture"], "units": "mm", "purpose": purpose,
                  "intrinsics": intrinsics, "frame": inventory["frame"],
                  "calibration_sha256": inventory["calibration_sha256"],
                  "tag_offset_mm": inventory["tag_offset_mm"], "objects": objects,
                  "placement_observation": inventory.get("placement_observation")}
        atomic_json(self.folder / f"detections_{inventory['capture']}.json", record)
        if purpose == "scan":
            atomic_json(self.folder / "latest.json", record)
        self._append_csv(objects, inventory["capture"])
        save_inventory(self.folder, inventory, latest=purpose == "scan")


    def _append_csv(self, objects, stamp):
        path = self.folder / "detections.csv"
        try:
            if path.exists():
                with path.open() as stream:
                    header = stream.readline().strip()
                if header != ",".join(CSV_FIELDS):
                    path.rename(self.folder / f"detections_old_{stamp}.csv")
            self._write_rows(path, objects, stamp)
        except PermissionError:
            spare = self.folder / f"detections_{stamp}.csv"
            log(f"{path.name} is locked; saving these rows to {spare.name}")
            self._write_rows(spare, objects, stamp)


    @staticmethod
    def _write_rows(path, objects, stamp):
        new_file = not path.exists()
        with path.open("a", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerows({"capture": stamp, **obj} for obj in objects)


class CaptureService:
    def __init__(self, camera, worker, calibration, grid, store, *, depth_frames=5,
                 scan_observations=1, confidence=0.5, table_z=0.0):
        if type(depth_frames) is not int or not 1 <= depth_frames <= 9:
            raise ValueError("Depth frame count must be 1..9.")
        if type(scan_observations) is not int or scan_observations not in (1, 3):
            raise ValueError("Scan observation count must be 1 or 3.")
        self.camera, self.worker, self.calibration = camera, worker, calibration
        self.grid, self.store, self.depth_frames = grid, store, depth_frames
        self.scan_observations = scan_observations
        self.confidence, self.table_z = confidence, table_z
        self.K = camera.intrinsics()
        if (type(self.K.get("width")) is not int or type(self.K.get("height")) is not int
                or not 0 < self.K["width"] <= 1920 or not 0 < self.K["height"] <= 1080):
            raise CameraError("Lynx native perception supports aligned frames up to 1920×1080.")
        self.reference_path = grid.path.with_suffix(".table-reference.json")
        # The seven-cell profile depends on its verified empty-table reference.
        # Require it explicitly instead of silently reverting to a new baseline.
        self.reference_raw = bounded_read(self.reference_path)
        self.reference = json.loads(self.reference_raw)
        self._config(grid.snapshot())

    def _check_identity(self):
        self.calibration.ensure_unchanged()
        if bounded_read(self.reference_path) != self.reference_raw:
            raise SelectionError("Empty-table reference changed. Restart and scan again.")

    def _config(self, snapshot):
        cell_size = snapshot.get("cell_size_mm", 80.0)
        validate_reference(self.reference, calibration=self.calibration, intrinsics=self.K,
                           locations=snapshot["locations"], cell_size=cell_size, table_z=self.table_z)
        return {"calibration": self.calibration.value, "intrinsics": self.K,
                "names": list(self.worker.model.names), "grid_snapshot": snapshot,
                "cell_size_mm": cell_size, "table_z_mm": self.table_z,
                "table_reference": self.reference}

    def _observe(self, config):
        self._check_identity()
        if self.camera.intrinsics() != self.K:
            raise CameraError("Camera intrinsics changed. Restart and scan again.")
        deadline, samples, metadata = time.monotonic() + 10.0, [], []
        for index in range(self.depth_frames):
            if index:
                del rgb, depth
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CameraError("Depth sample collection timed out.")
            rgb, depth, frame = self.camera.capture_raw(timeout_s=remaining)
            sequence = frame.get("sequence")
            if type(sequence) is not int or (metadata and sequence <= metadata[-1]["sequence"]):
                raise CameraError("Depth samples must have advancing frame sequences; scan again.")
            if (frame.get("width"), frame.get("height")) != (self.K["width"], self.K["height"]):
                raise CameraError("Image dimensions do not match the camera intrinsics.")
            samples.append(depth)
            metadata.append(frame)
        frame = dict(metadata[-1])
        frame["observation_started_monotonic"] = metadata[0]["observation_started_monotonic"]
        if len(samples) > 1:
            frame["depth_sampling"] = {
                "count": len(samples), "method": "mean_of_stable_object_core_depths",
                "sequences": [item["sequence"] for item in metadata],
                "timestamps": [item["timestamp"] for item in metadata],
                "grid_evidence": "latest_raw_aligned_frame"}
        if self.camera.intrinsics() != self.K:
            raise CameraError("Camera intrinsics changed during the depth burst. Restart and scan again.")
        self._check_identity()
        # observe consumes samples. The latest depth alone remains for auditing.
        result = self.worker.observe(rgb, samples, config)
        self._check_identity()
        if self.camera.intrinsics() != self.K:
            raise CameraError("Camera intrinsics changed during perception. Restart and scan again.")
        return rgb, depth, frame, result

    def capture(self, purpose):
        self._check_identity()
        snapshot = self.grid.snapshot()
        config = self._config(snapshot)
        observations, sequences, model_results = [], [], []
        count = self.scan_observations if purpose == "scan" else 1
        for index in range(count):
            if index:
                del rgb, depth, result
            rgb, depth, frame, result = self._observe(config)
            observations.append(result["slots"])
            sequences.append(frame["sequence"])
            if len(sequences) > 1 and sequences[-1] <= sequences[-2]:
                raise CameraError("Placement checks require advancing camera observations.")
            model_results.append({"backend": "ncnn-cpp", "precision": self.worker.model.precision,
                                  "model_sha256": self.worker.model.sha256,
                                  "inference_ms": result["inference_ms"]})
        self._check_identity()
        # Occupancy can change through reservations; geometry may not change
        # halfway through a capture. Revalidate against the frozen reference.
        self._config(self.grid.snapshot())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + "_" + purpose
        images = {kind: str(self.store.folder / f"{kind}_{stamp}.png") for kind in ("rgb", "depth")}
        # Command-only mode stores the original image; IDs and cell polygons
        # are recorded in JSON. It does not load annotation or UI libraries.
        images["annotated"] = images["rgb"]
        inventory = build_inventory(result["objects"], capture_id=stamp, frame=frame,
                                    calibration_id=self.calibration.digest,
                                    tag_offset=self.calibration.offset, gateway=self.camera.api,
                                    images=images, model={"weights": str(self.worker.model.weights),
                                    "confidence_threshold": self.confidence,
                                    "inference_ms": result["inference_ms"],
                                    "inference": model_results[-1], "observation_results": model_results})
        inventory["images"]["annotation_status"] = "raw image; cup IDs and grid polygons are in JSON"
        exclude_placed_cups(inventory["cups"], snapshot["locations"], config["cell_size_mm"], self.calibration.offset)
        observation = (combine(snapshot, observations, sequences) if purpose == "scan" else
                       {"frame_count": 1, "slots": observations[-1], "applied": False,
                        "released_ids": [], "message": "Verification image only; placement status unchanged."})
        observation["cell_size_mm"] = config["cell_size_mm"]
        observation["table_reference"] = {
            "source_capture": self.reference["source"]["capture"],
            "source_depth_sha256": self.reference["source"]["depth_sha256"],
            "plane_z_mm": copy.deepcopy(self.reference["plane_z_mm"]),
            "fit_rms_mm": self.reference["fit"]["rms_mm"],
            "file_sha256": hashlib.sha256(self.reference_raw).hexdigest()}
        inventory["placement_observation"] = observation
        self.store.save_capture(inventory, rgb, depth, result["objects"], self.K, purpose)
        print(f"[{purpose}] {len(inventory['cups'])} cup candidates; native inference {result['inference_ms']:.0f} ms", flush=True)
        return inventory, frame["observation_started_monotonic"]
