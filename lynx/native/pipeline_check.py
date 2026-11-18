"""Offline end-to-end CLI perception check using saved RGB/depth.

The repeated depth samples are synthetic replay, not new camera evidence.
This tool never creates a RobotClient or sends network/robot requests.
It requires NumPy/OpenCV only on the development host to decode the fixture.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import cv2
from lynx.capture import Calibration, CaptureService, CaptureStore
from lynx.model import Model
from lynx.native_perception import NativePerception
from placing_grid import PlacingGrid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--detections", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    stamp = args.detections.name.removeprefix("detections_").removesuffix(".json")
    source = json.loads(args.detections.read_text())
    rgb = cv2.imread(str(args.detections.parent / f"rgb_{stamp}.jpg"))
    depth = cv2.imread(str(args.detections.parent / f"depth_{stamp}.png"), cv2.IMREAD_UNCHANGED)
    if rgb is None or depth is None:
        raise ValueError("Saved RGB/depth pair is missing.")
    color_bytes, depth_bytes = rgb.transpose(2, 0, 1).tobytes(), depth.astype("<u2").tobytes()
    del rgb, depth
    class SavedCamera:
        api = "offline://saved-fixture"
        sequence = 0
        def intrinsics(self):
            return source["intrinsics"]
        def capture_raw(self, timeout_s=10):
            self.sequence += 1
            started = time.monotonic()
            return color_bytes, depth_bytes, {"sequence": self.sequence,
                "width": source["intrinsics"]["width"], "height": source["intrinsics"]["height"],
                "timestamp": "offline-replay", "observed_at": "offline-replay",
                "observation_started_monotonic": started, "received_monotonic": started}
    # Preserve source occupancy, reservation and recovery data; only scratch is written.
    grid_path = args.output / "placing-grid.json"
    shutil.copyfile(args.grid, grid_path)
    shutil.copyfile(args.grid.with_suffix(".table-reference.json"), grid_path.with_suffix(".table-reference.json"))
    before = grid_path.read_bytes()
    worker = NativePerception(args.worker, Model.load(args.model), timeout=60)
    try:
        store = CaptureStore(args.output / "captures")
        service = CaptureService(SavedCamera(), worker, Calibration(args.calibration), PlacingGrid(grid_path), store)
        inventory, started = service.capture("scan")
        assert grid_path.read_bytes() == before, "Camera-only replay changed placement records"
        report = {"source": stamp, "synthetic_repeated_depth": True, "robot_commands": 0,
                  "capture": inventory["capture"], "cups": len(inventory["cups"]),
                  "slots": [{"id": s["id"], "state": s["state"]} for s in inventory["placement_observation"]["slots"]],
                  "model": inventory["model"], "last_observation_age_s": time.monotonic() - started,
                  "grid_sha256_unchanged": hashlib.sha256(before).hexdigest()}
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        worker.close()


if __name__ == "__main__":
    main()
