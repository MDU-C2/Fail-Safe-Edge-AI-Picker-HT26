"""Project placement cells and require positive table-depth evidence to clear them.

No file writes or robot calls. A Scan combines three fresh RGB/depth observations;
verification captures draw the overlay but never change placement bookkeeping.
"""

import copy
import math

import cv2
import numpy as np

from cup_inventory import SelectionError, finite_number

SCAN_FRAMES = 3
TABLE_TOLERANCE_MM = 20.0
CHECK_HEIGHT_MM = 160.0
MIN_COVERAGE = 0.995
MIN_CLUSTER_PIXELS = 8
# Four float64 corner rays and their slab-intersection scratch arrays are much
# larger than the final boolean mask. Bound their working set independently of
# the projected placement cell's area (at most 8192 pixels per ordinary row block).
RAY_CHUNK_PIXELS = 8192


class PlacementVision:
    def __init__(self, calibration, intrinsics, *, table_z=0.0, cell_size=80.0):
        if not finite_number(table_z) or not finite_number(cell_size) or not 20 <= cell_size <= 200:
            raise ValueError("Grid table Z must be finite; cell size must be 20..200 mm.")
        self.calibration, self.K = calibration, intrinsics
        self.table_z, self.cell_size = float(table_z), float(cell_size)
        self.transform = calibration.scale * np.asarray(calibration.R)
        self.translation = np.asarray(calibration.t) + np.asarray(calibration.offset)
        if abs(np.linalg.det(self.transform)) < 1e-9:
            raise ValueError("Calibration cannot be inverted for the placement overlay.")
        self.min_depth_scale = float(np.linalg.svd(self.transform, compute_uv=False).min())

    def project(self, points):
        # Calibration reports a TCP-relative tag location; all three tag-offset
        # components are needed to recover physical points on the table.
        camera = np.linalg.solve(self.transform, (np.asarray(points) - self.translation).T).T
        if not np.isfinite(camera).all() or np.any(camera[:, 2] <= 0):
            return None
        return np.column_stack((camera[:, 0] * self.K["fx"] / camera[:, 2] + self.K["cx"],
                                camera[:, 1] * self.K["fy"] / camera[:, 2] + self.K["cy"]))

    def polygons(self, location):
        half = self.cell_size / 2
        corners = [(location["x"] + dx, location["y"] + dy, self.table_z)
                   for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half))]
        floor = self.project(corners)
        # Include the projected volume above the cell. At the side of the image
        # a cup's rim can project outside its table footprint (parallax).
        volume = self.project(corners + [(x, y, z + CHECK_HEIGHT_MM) for x, y, z in corners])
        return floor, None if volume is None else cv2.convexHull(volume.astype(np.float32)).reshape(-1, 2)

    @staticmethod
    def _cluster(mask):
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        return count > 1 and int(stats[1:, cv2.CC_STAT_AREA].max()) >= MIN_CLUSTER_PIXELS

    def _clearly_behind_volume(self, depth, u, v, location):
        """Exclude only surfaces seen beyond the entire cell along their rays.

        Physical XY alone cannot distinguish a neighbouring object behind the
        cell from one hiding it in front. Intersect each camera ray with the
        physical cell prism, keeping depth in the camera's optical-Z units.
        Four pixel corners and the existing physical-distance tolerance keep
        silhouettes, grazing rays and uncertain boundaries conservative.
        """
        if depth.size <= RAY_CHUNK_PIXELS:
            return self._clearly_behind_volume_chunk(depth, u, v, location)
        rows = max(1, RAY_CHUNK_PIXELS // depth.shape[1])
        result = np.empty(depth.shape, dtype=bool)
        for start in range(0, depth.shape[0], rows):
            end = start + rows
            result[start:end] = self._clearly_behind_volume_chunk(
                depth[start:end], u[start:end], v[start:end], location)
        return result

    def _clearly_behind_volume_chunk(self, depth, u, v, location):
        # Keep all original float64 operations and conservative boundary tests.
        # Every calculation is per pixel: no reductions cross a row block.
        corners = np.array([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]])
        rays = np.stack(((u[..., None] + corners[:, 0] - self.K["cx"]) / self.K["fx"],
                         (v[..., None] + corners[:, 1] - self.K["cy"]) / self.K["fy"],
                         np.ones(depth.shape + (4,))), axis=-1) @ self.transform.T
        half = self.cell_size / 2
        lower = (location["x"] - half, location["y"] - half, self.table_z)
        upper = (location["x"] + half, location["y"] + half, self.table_z + CHECK_HEIGHT_MM)
        enter = np.zeros(depth.shape + (4,))
        leave = np.full_like(enter, np.inf)
        exit_bound = np.full(depth.shape, np.inf)
        for axis in range(3):
            direction = rays[..., axis]
            parallel = np.abs(direction) < 1e-12
            first, last = np.zeros_like(direction), np.zeros_like(direction)
            np.divide(lower[axis] - self.translation[axis], direction, out=first, where=~parallel)
            np.divide(upper[axis] - self.translation[axis], direction, out=last, where=~parallel)
            near, far = np.minimum(first, last), np.maximum(first, last)
            inside = lower[axis] <= self.translation[axis] <= upper[axis]
            near = np.where(parallel, -np.inf if inside else np.inf, near)
            far = np.where(parallel, np.inf if inside else -np.inf, far)
            enter, leave = np.maximum(enter, near), np.minimum(leave, far)
            # Bound every ray within the pixel, including where its exit face
            # changes. A direction crossing zero has no finite bound on this
            # axis; another slab must establish the exit instead.
            crosses_zero = (direction.min(axis=-1) <= 1e-12) & (direction.max(axis=-1) >= -1e-12)
            axis_bound = np.where(crosses_zero, np.inf, far.max(axis=-1))
            exit_bound = np.minimum(exit_bound, axis_bound)
        intersects = np.all(np.isfinite(enter) & np.isfinite(leave) & (leave > enter), axis=-1)
        # A point above the checked height but still over the destination must
        # not become clear for a low or side-looking camera configuration.
        physical_xy = self.translation[:2] + rays[..., :2] * depth[..., None, None]
        outside_xy = np.any(np.all(physical_xy < lower[:2], axis=-2)
                            | np.all(physical_xy > upper[:2], axis=-2), axis=-1)
        # The minimum singular value is a lower bound on physical distance per
        # unit of optical Z for every ray, including rays inside a pixel.
        margin = TABLE_TOLERANCE_MM / self.min_depth_scale
        return (np.isfinite(depth) & (depth > 0) & intersects & outside_xy & np.isfinite(exit_bound)
                & (depth > exit_bound + margin))

    def exclude_placed_cups(self, cups, locations):
        """A cup already in the destination area must not enter a pick-all batch."""
        half = self.cell_size / 2
        for cup in cups:
            if not all(finite_number(cup.get(axis)) for axis in ("x", "y")):
                continue
            x, y = cup["x"] + self.calibration.offset[0], cup["y"] + self.calibration.offset[1]
            ids = [place["id"] for place in locations
                   if abs(place["x"] - x) <= half and abs(place["y"] - y) <= half]
            if ids:
                cup.update(selectable=False, status="blocked", placement_location_ids=ids,
                           blocked_reason="Cup is already in the placement area; remove it by hand before reusing the location.")

    def assess(self, depth, objects, locations):
        height, width = depth.shape
        if (height, width) != (self.K["height"], self.K["width"]):
            raise SelectionError("Placement depth image does not match camera intrinsics.")
        slots = []
        for location in locations:
            floor, volume = self.polygons(location)
            slot = {"id": location["id"], "state": "unknown", "reason": "Placement area is outside the camera view.",
                    "polygon_px": [] if floor is None else floor.round(2).tolist(), "valid_fraction": 0.0,
                    "reserved": location.get("reserved", False)}
            slots.append(slot)
            if (volume is None or np.any(volume[:, 0] < 1) or np.any(volume[:, 1] < 1)
                    or np.any(volume[:, 0] >= width - 1) or np.any(volume[:, 1] >= height - 1)):
                continue
            x0, y0 = np.floor(volume.min(axis=0)).astype(int)
            x1, y1 = np.ceil(volume.max(axis=0)).astype(int) + 1
            mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
            cv2.fillConvexPoly(mask, np.rint(volume - [x0, y0]).astype(np.int32), 1)
            roi = mask.astype(bool)
            floor_mask = np.zeros_like(mask)
            cv2.fillConvexPoly(floor_mask, np.rint(floor - [x0, y0]).astype(np.int32), 1)
            floor_roi = floor_mask.astype(bool)
            if roi.sum() < 100:
                slot["reason"] = "Placement area is too small in this image."
                continue
            # Include all cup detections, even ones rejected for robot picking.
            detected = False
            for obj in objects:
                box = obj.get("bbox_xyxy")
                if obj.get("class") not in ("CUP", "CUP BASE", "CUP HANDLE"):
                    continue
                if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(finite_number(v) for v in box):
                    continue
                a, b = max(0, math.floor(box[0]) - x0), max(0, math.floor(box[1]) - y0)
                c, d = min(x1 - x0, math.ceil(box[2]) - x0 + 1), min(y1 - y0, math.ceil(box[3]) - y0 + 1)
                if a < c and b < d and roi[b:d, a:c].any():
                    detected = True
                    break
            z = depth[y0:y1, x0:x1].astype(float)
            valid = roi & np.isfinite(z) & (z > 0)
            fraction = float(valid.sum() / roi.sum())
            slot["valid_fraction"] = round(fraction, 4)
            yy, xx = np.indices(z.shape)
            # Physical robot height for every aligned depth pixel, including the
            # vertical tag offset. A median alone could miss a cup rim.
            rz = (self.transform[2, 0] * (xx + x0 - self.K["cx"]) / self.K["fx"]
                  + self.transform[2, 1] * (yy + y0 - self.K["cy"]) / self.K["fy"]
                  + self.transform[2, 2]) * z + self.translation[2] - self.table_z
            high = valid & (rz > TABLE_TOLERANCE_MM)
            high &= ~self._clearly_behind_volume(z, xx + x0, yy + y0, location)
            # Confirm the table across the actual cell footprint. The wider
            # volume still vetoes obstacles, foreground occlusion and missing
            # depth. Surfaces demonstrably behind it need not be coplanar.
            off_table = valid & floor_roi & (np.abs(rz) > TABLE_TOLERANCE_MM)
            table_fraction = float((valid & floor_roi & ~off_table).sum() / floor_roi.sum())
            slot["table_fraction"] = round(table_fraction, 4)
            if detected or self._cluster(high):
                slot.update(state="occupied", reason="Cup or raised object visible in the placement area.")
            elif (fraction < MIN_COVERAGE or self._cluster(roi & ~valid)):
                slot["reason"] = "Not enough valid depth to confirm an empty table."
            elif table_fraction < MIN_COVERAGE or self._cluster(off_table):
                slot["reason"] = "Depth does not consistently match the calibrated table plane."
            else:
                slot.update(state="clear", reason="Clear table depth observed; no overlapping cup detection.")
        return slots

    @staticmethod
    def combine(snapshot, observations, sequences):
        if (len(observations) != SCAN_FRAMES or len(sequences) != SCAN_FRAMES
                or any(type(s) is not int or s < 0 for s in sequences)
                or any(a >= b for a, b in zip(sequences, sequences[1:]))):
            raise SelectionError("Placement check needs three distinct advancing camera frames. Scan again.")
        expected_ids = [location["id"] for location in snapshot["locations"]]
        if (any([slot["id"] for slot in observation] != expected_ids for observation in observations)
                or any([slot["polygon_px"] for slot in observation] !=
                       [slot["polygon_px"] for slot in observations[0]] for observation in observations)):
            raise SelectionError("Placement geometry changed during the scan.")
        slots = copy.deepcopy(observations[-1])
        for index, slot in enumerate(slots):
            checks = [observation[index] for observation in observations]
            if any(check["id"] != slot["id"] for check in checks):
                raise SelectionError("Placement geometry changed during the scan.")
            if any(check["state"] == "occupied" for check in checks):
                slot.update(state="occupied", reason="Cup or raised object seen during this scan.")
            elif not all(check["state"] == "clear" for check in checks):
                slot.update(state="unknown", reason="Three clear depth observations were not obtained.")
            slot["valid_fraction"] = min(check["valid_fraction"] for check in checks)
        return {"frame_count": SCAN_FRAMES, "frame_sequences": list(sequences), "slots": slots,
                "grid_snapshot": snapshot, "applied": False, "released_ids": [],
                "message": "Placement area checked against three fresh depth observations."}

    @staticmethod
    def draw(image, slots):
        colors = {"clear": (80, 205, 90), "occupied": (60, 85, 240), "unknown": (155, 155, 155)}
        for slot in slots:
            polygon = np.asarray(slot["polygon_px"], dtype=float)
            if polygon.shape != (4, 2) or not np.isfinite(polygon).all() or np.abs(polygon).max() > 100000:
                continue
            polygon = np.rint(polygon).astype(np.int32)
            reserved = slot.get("reserved", False)
            color = (0, 185, 255) if reserved else colors[slot["state"]]
            cv2.polylines(image, [polygon], True, color, 2, cv2.LINE_AA)
            center = np.rint(polygon.mean(axis=0)).astype(int)
            label = f"P{slot['id']}"
            state = "RESERVED" if reserved else slot["state"].upper()
            for text, dy, scale in ((label, -2, 0.75), (state, 19, 0.38)):
                size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0]
                pos = (int(center[0] - size[0] / 2), int(center[1] + dy))
                cv2.putText(image, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(image, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)
        return image
