"""Bounded observed depth uncertainty beside confidently located cups.

This deliberately accepts that a small unlabelled object beside a cup can be
missed. Image and physical proximity are attribution heuristics, not proof of
object identity. The helper grants no individual depth ownership and never
fills missing depth. Exact detections, enclosed cup holes, taller foreground
surfaces and samples beyond the local envelope remain ordinary depth evidence.
Callers must keep each possible owner's cell occupied, protect other object
detections, subtract these samples from visible-area accounting, and retain all
remaining obstacle, missing-depth and minimum-visible-table checks.
"""

import cv2
import numpy as np

from cup_inventory import finite_number
from detection_masks import decode_mask


MAX_FRAME_PIXELS = 8_388_608
MAX_CROP_PIXELS = 262_144
MAX_OBJECTS = 128
MAX_CUPS = 32
MAX_LOCATIONS = 128
MAX_OWNER_COMBINATIONS = 256
MAX_WORK_PIXELS = 4 * MAX_FRAME_PIXELS
MAX_PAD_PIXELS = 160
MIN_SEED_PIXELS = 32
MIN_VALID_FRACTION = .95
DEFAULT_RADIUS_MM = 40.
DEFAULT_TAIL_RADIUS_MM = 80.
TAIL_HEIGHT_MM = 70.
HEIGHT_MARGIN_MM = 5.
CUP_CLASSES = frozenset({"CUP", "CUP BASE", "CUP HANDLE"})


def _holes(mask):
    background = np.pad((~mask).astype(np.uint8), 1, constant_values=1)
    cv2.floodFill(background, None, (0, 0), 2, flags=8)
    return background[1:-1, 1:-1] == 1


def _box(obj, shape):
    value = obj.get("bbox_xyxy")
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or not all(finite_number(v) for v in value)
            or value[2] <= value[0] or value[3] <= value[1]):
        return None
    height, width = shape
    x0, y0 = max(0, int(np.floor(value[0]))), max(0, int(np.floor(value[1])))
    x1, y1 = min(width, int(np.ceil(value[2])) + 1), min(height, int(np.ceil(value[3])) + 1)
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def _near_hull(points, hull, radius):
    """Vectorized distance to a convex outline, without an NxMx2 allocation."""
    polygon = hull.reshape(-1, 2).astype(float)
    positive = np.zeros(len(points), bool)
    negative = np.zeros(len(points), bool)
    closest = np.full(len(points), np.inf)
    for start, end in zip(polygon, np.roll(polygon, -1, axis=0)):
        edge = end - start
        length = float(edge @ edge)
        if length <= 0:
            continue
        offset = points - start
        cross = edge[0] * offset[:, 1] - edge[1] * offset[:, 0]
        positive |= cross > 1e-9
        negative |= cross < -1e-9
        along = np.clip((offset @ edge) / length, 0., 1.)
        delta = offset - along[:, None] * edge
        closest = np.minimum(closest, np.sum(delta * delta, axis=1))
    return ~(positive & negative) | (closest <= radius * radius + 1e-9)


def cup_uncertainty_regions(depth, objects, locations, assignments, *, transform,
                            translation, intrinsics, cell_size, table_z=0.,
                            check_height=160., radius_mm=DEFAULT_RADIUS_MM,
                            tail_radius_mm=DEFAULT_TAIL_RADIUS_MM):
    """Return ``(regions, evidence)`` without changing any input.

    ``regions`` contains ``(frozenset(one_based_location_indices), (rows, cols))``.
    Overlapping envelopes combine their possible owner sets. ``evidence`` is a
    JSON-compatible list with one record per cup contributing uncertain pixels.

    Exact masks establish each cup independently: >=32 usable pixels, >=95%
    valid/plausible depth, and the same unique physical-pixel majority in its
    recorded cell used by the exact-mask cup assignment. Every returned sample
    is observed above 30 mm, no higher than
    160 mm (or the supplied limit), and no higher than its cup's observed top
    plus 5 mm. Both perspective-scaled image distance and XY outline distance
    must be within the same cup's radius. A separately counted low foreground
    tail may extend to 80 mm only while its observed physical XY lies outside
    EVERY grid cell and its height is at most 70 mm (and below the cup ceiling).
    It must connect to the same cup's raised exact seeds through observed raised
    depth inside both 80 mm bounds, without crossing protected detections or
    holes. The path may pass through another cell, but those path pixels receive
    no wider exemption. This tail exemption never reaches into another cell's
    physical footprint.
    No whole-component seed fraction or
    connected-component membership is required; detached small edge fragments
    may qualify. Samples inside any exact mask or enclosed cup hole never do.

    Original ownership is deliberately not an input: uncertainty may cover a
    fringe-only conflict, but exact-mask overlaps remain protected. Limits bound
    masks, per-cup crops, padding and aggregate work. Unsupported cups are skipped;
    malformed global inputs or an exceeded aggregate budget return no exemptions.
    """
    empty = ([], [])
    if (not isinstance(depth, np.ndarray) or depth.ndim != 2
            or not np.issubdtype(depth.dtype, np.number) or np.iscomplexobj(depth)
            or not 0 < depth.size <= MAX_FRAME_PIXELS
            or not isinstance(objects, (list, tuple)) or len(objects) > MAX_OBJECTS
            or any(not isinstance(o, dict) or not isinstance(o.get("class"), str) for o in objects)
            or not isinstance(assignments, (list, tuple)) or not 0 < len(assignments) <= MAX_CUPS
            or any(not isinstance(a, dict) for a in assignments)
            or not isinstance(locations, (list, tuple)) or not 0 < len(locations) <= MAX_LOCATIONS):
        return empty
    try:
        matrix, origin = np.asarray(transform, dtype=float), np.asarray(translation, dtype=float)
        fx, fy, cx, cy = (intrinsics[key] for key in ("fx", "fy", "cx", "cy"))
        if (matrix.shape != (3, 3) or origin.shape != (3,)
                or not np.isfinite(matrix).all() or not np.isfinite(origin).all()
                or not all(finite_number(v) for v in (fx, fy, cx, cy, cell_size, table_z,
                                                      check_height, radius_mm, tail_radius_mm))
                or fx <= 0 or fy <= 0 or not 20 <= cell_size <= 200
                or not 30 < check_height <= 160 or not 0 < radius_mm <= 40
                or not radius_mm <= tail_radius_mm <= 80
                or any(not isinstance(p, dict) or type(p.get("id")) is not int
                       or not all(finite_number(p.get(axis)) for axis in ("x", "y")) for p in locations)):
            return empty
        scale = float(np.linalg.svd(matrix, compute_uv=False).min())
        if not np.isfinite(scale) or scale <= 1e-12:
            return empty
    except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
        return empty
    places = {p["id"]: i for i, p in enumerate(locations)}
    if len(places) != len(locations):
        return empty
    height, width = depth.shape
    decoded, work = [], 0
    protected = np.zeros(depth.shape, bool)
    for obj in objects:
        region = decode_mask(obj.get("segmentation"), depth.shape)
        decoded.append(region)
        if region is None:
            box = _box(obj, depth.shape)
            if box is None:
                return empty  # An unlocatable labelled detection cannot be protected.
            x0, y0, x1, y1 = box
            protected[y0:y1, x0:x1] = True
            work += (y1-y0) * (x1-x0)
        else:
            x, y, mask = region
            protected[y:y+mask.shape[0], x:x+mask.shape[1]] |= mask
            work += mask.size
        if work > MAX_WORK_PIXELS:
            return empty

    def physical(rows, columns):
        optical = depth[rows, columns].astype(float)
        rays = np.column_stack(((columns-cx)/fx, (rows-cy)/fy, np.ones(len(rows))))
        with np.errstate(over="ignore", invalid="ignore"):
            return (rays @ matrix.T) * optical[:, None] + origin

    # Prepare all masks before attribution so another cup's hole/exact mask can
    # never be consumed by an earlier cup's envelope.
    groups, claimed_indices = [], set()
    for record in assignments:
        indices, place_id = record.get("object_indices"), record.get("location_id")
        if (type(place_id) is not int or place_id not in places
                or not isinstance(indices, (list, tuple)) or not indices
                or any(type(i) is not int or not 0 <= i < len(objects) for i in indices)
                or len(set(indices)) != len(indices) or claimed_indices.intersection(indices)):
            return empty
        claimed_indices.update(indices)
        if (any(decoded[i] is None or objects[i]["class"] not in CUP_CLASSES
                or not finite_number(objects[i].get("confidence"))
                or not .5 <= objects[i]["confidence"] <= 1 for i in indices)):
            continue
        x0, y0 = min(decoded[i][0] for i in indices), min(decoded[i][1] for i in indices)
        x1 = max(decoded[i][0] + decoded[i][2].shape[1] for i in indices)
        y1 = max(decoded[i][1] + decoded[i][2].shape[0] for i in indices)
        if (x1-x0) * (y1-y0) > MAX_CROP_PIXELS:
            continue
        mask = np.zeros((y1-y0, x1-x0), bool)
        for i in indices:
            x, y, part = decoded[i]
            mask[y-y0:y-y0+part.shape[0], x-x0:x-x0+part.shape[1]] |= part
        protected[y0:y1, x0:x1] |= _holes(mask)
        work += mask.size * 2
        if work > MAX_WORK_PIXELS:
            return empty
        groups.append((record, x0, y0, mask))

    memberships = np.zeros(depth.shape, np.uint32)
    evidence, contributing = [], []
    half = cell_size / 2
    for record, x0, y0, mask in groups:
        sy, sx = np.nonzero(mask)
        if len(sy) < MIN_SEED_PIXELS:
            continue
        optical = depth[sy+y0, sx+x0].astype(float)
        points = physical(sy+y0, sx+x0)
        valid = (np.isfinite(optical) & (optical > 0) & np.isfinite(points).all(axis=1)
                 & (np.abs(points[:, :2]) < 2**40).all(axis=1)
                 & (points[:, 2] >= table_z-30.) & (points[:, 2] <= table_z+check_height))
        if valid.sum() < MIN_SEED_PIXELS or valid.mean() < MIN_VALID_FRACTION:
            continue
        seed_points = points[valid]
        if np.count_nonzero(seed_points[:, 2] > table_z+30.) < MIN_SEED_PIXELS:
            continue
        scores = np.array([np.count_nonzero((np.abs(seed_points[:, 0]-p["x"]) <= half)
                                            & (np.abs(seed_points[:, 1]-p["y"]) <= half)) for p in locations])
        owner = places[record["location_id"]]
        if (scores[owner] < len(seed_points) * .5 or scores[owner] <= 0
                or np.any(np.delete(scores, owner) >= scores[owner])):
            continue
        hull = cv2.convexHull(seed_points[:, :2].astype(np.float32))
        if len(hull) < 3 or cv2.contourArea(hull) <= 0:
            continue
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            projected_pad = tail_radius_mm * max(fx, fy) / (scale * optical[valid].min())
        if not np.isfinite(projected_pad) or projected_pad > MAX_PAD_PIXELS - 2:
            continue
        pad = int(np.ceil(projected_pad)) + 2
        left, top = max(0, x0-pad), max(0, y0-pad)
        right, bottom = min(width, x0+mask.shape[1]+pad), min(height, y0+mask.shape[0]+pad)
        size = (right-left) * (bottom-top)
        if size > MAX_CROP_PIXELS:
            continue
        work += size * 6
        if work > MAX_WORK_PIXELS:
            return empty
        seed = np.zeros((bottom-top, right-left), bool)
        seed[sy[valid]+y0-top, sx[valid]+x0-left] = True
        distance = cv2.distanceTransform((~seed).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        crop = depth[top:bottom, left:right].astype(float)
        with np.errstate(over="ignore", invalid="ignore"):
            image_distance = distance * crop / max(fx, fy) * scale
            near = image_distance <= tail_radius_mm
        selected = near & np.isfinite(crop) & (crop > 0) & ~protected[top:bottom, left:right]
        rows, columns = np.nonzero(selected)
        if not len(rows):
            continue
        world = physical(rows+top, columns+left)
        ceiling = min(table_z+check_height, float(seed_points[:, 2].max()) + HEIGHT_MARGIN_MM)
        plausible = (np.isfinite(world).all(axis=1) & (world[:, 2] > table_z+30.)
                     & (world[:, 2] <= ceiling) & (np.abs(world[:, :2]) < 2**40).all(axis=1))
        edge = plausible & (image_distance[rows, columns] <= radius_mm)
        edge[edge] = _near_hull(world[edge, :2], hull, radius_mm)
        outside_grid = np.ones(len(rows), bool)
        for place in locations:
            outside_grid &= ((np.abs(world[:, 0]-place["x"]) > half)
                             | (np.abs(world[:, 1]-place["y"]) > half))
        tail = plausible & ~edge & outside_grid & (world[:, 2] <= table_z+TAIL_HEIGHT_MM)
        tail[tail] = _near_hull(world[tail, :2], hull, tail_radius_mm)
        if tail.any():
            # Larger projected tails need an observed path to this exact cup.
            # The small local band deliberately permits disconnected fragments.
            within = np.zeros(len(rows), bool)
            within[plausible] = _near_hull(world[plausible, :2], hull, tail_radius_mm)
            path = np.zeros(crop.shape, bool)
            path[rows[within], columns[within]] = True
            anchors = np.zeros(crop.shape, bool)
            raised_seed = ((seed_points[:, 2] > table_z+30.) & (seed_points[:, 2] <= ceiling))
            anchors[sy[valid][raised_seed]+y0-top, sx[valid][raised_seed]+x0-left] = True
            # Own exact seeds are the only protected samples allowed to anchor
            # this path. Exact overlaps with any other object remain protected.
            for index, region in enumerate(decoded):
                if index in record["object_indices"]:
                    continue
                if region is None:
                    bx0, by0, bx1, by1 = _box(objects[index], depth.shape)
                    a, b, c, d = max(left, bx0), max(top, by0), min(right, bx1), min(bottom, by1)
                    if a < c and b < d:
                        anchors[b-top:d-top, a-left:c-left] = False
                        work += (d-b) * (c-a)
                else:
                    bx, by, part = region
                    a, b = max(left, bx), max(top, by)
                    c, d = min(right, bx+part.shape[1]), min(bottom, by+part.shape[0])
                    if a < c and b < d:
                        anchors[b-top:d-top, a-left:c-left] &= ~part[b-by:d-by, a-bx:c-bx]
                        work += (d-b) * (c-a)
                if work > MAX_WORK_PIXELS:
                    return empty
            if anchors.any():
                path |= anchors
                _, connected = cv2.connectedComponents(path.astype(np.uint8), connectivity=8)
                anchor_labels = np.unique(connected[anchors])
                anchor_labels = anchor_labels[anchor_labels != 0]
                tail &= np.isin(connected[rows, columns], anchor_labels)
            else:
                tail[:] = False
        accepted = edge | tail
        rows, columns = rows[accepted]+top, columns[accepted]+left
        if not len(rows):
            continue
        bit = np.uint32(1 << len(contributing))
        memberships[rows, columns] |= bit
        contributing.append(owner + 1)
        evidence.append({"location_id": record["location_id"],
                         "object_indices": list(record["object_indices"]),
                         "uncertain_pixels": int(len(rows)), "radius_mm": float(radius_mm),
                         "edge_uncertain_pixels": int(edge.sum()),
                         "projected_tail_pixels": int(tail.sum()),
                         "tail_radius_mm": float(tail_radius_mm),
                         "tail_maximum_height_mm": float(min(TAIL_HEIGHT_MM, ceiling-table_z)),
                         "maximum_height_mm": float(ceiling-table_z),
                         "seed_pixels": int(valid.sum()),
                         "seed_overlap_fraction": float(scores[owner] / len(seed_points)),
                         "method": "observed_cup_local_uncertainty"})
    rows, columns = np.nonzero(memberships)
    if not len(rows):
        return empty
    values = memberships[rows, columns]
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    boundaries = np.r_[0, np.flatnonzero(sorted_values[1:] != sorted_values[:-1]) + 1, len(order)]
    if len(boundaries) - 1 > MAX_OWNER_COMBINATIONS:
        return empty
    regions = []
    for start, end in zip(boundaries, boundaries[1:]):
        selected = order[start:end]
        value = sorted_values[start]
        owners = frozenset(owner for i, owner in enumerate(contributing) if int(value) & (1 << i))
        regions.append((owners, (rows[selected], columns[selected])))
    return regions, evidence
