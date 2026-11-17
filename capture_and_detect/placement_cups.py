"""Assign segmented cups to their dominant physical placement cell.

Only identified cup pixels receive an owner. Boxes never erase depth evidence,
and unsupported parts or ambiguous associations remain ordinary obstacles.
"""

import cv2
import numpy as np

from cup_inventory import SelectionError, finite_number
from detection_masks import decode_mask

CUP_CLASSES = {"CUP", "CUP BASE", "CUP HANDLE"}
MIN_CONFIDENCE = 0.5
MIN_CUP_PIXELS = 32
MIN_USABLE_DEPTH = 0.95
MIN_DOMINANT_FRACTION = 0.5
DEPTH_FRINGE_MM = 20.0
NARROW_DEPTH_FRINGE_MM = 10.0
MAX_SURFACE_STEP_MM = 12.0
MAX_FRINGE_FRACTION = 0.25
MAX_FRINGE_PIXELS = 131072


def _box(obj):
    box = obj.get("bbox_xyxy")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(finite_number(v) for v in box)
            or box[2] <= box[0] or box[3] <= box[1]):
        return None
    return box


def _inside_part(part, parent, *, handle=False):
    """Associate a part only when exactly one body can contain it."""
    margin = 0.15 * max(parent[2] - parent[0], parent[3] - parent[1]) if handle else 0.0
    x, y = (part[0] + part[2]) / 2, (part[1] + part[3]) / 2
    return parent[0] - margin <= x <= parent[2] + margin and parent[1] - margin <= y <= parent[3] + margin


def _depth_fringe(depth, objects, indices, mask, x0, y0, physical_seed, *, transform,
                  translation, intrinsics, table_z, check_height):
    """Recover a small connected depth outline outside a segmented cup.

    Aligned depth can have a coarser/offset silhouette than RGB. Recovery needs
    a compact XY envelope, a short image margin, continuous raised surface,
    supported height and a small area. It never fills mask holes or grows
    through other detections. Indistinguishable touching unlabelled objects
    cannot be resolved from this depth observation alone.

    If the whole 20 mm outline exceeds the area budget, recompute once within
    10 mm. Never trim a candidate by pixel count or relax another validation.
    """
    geometry = dict(transform=transform, translation=translation, intrinsics=intrinsics,
                    table_z=table_z, check_height=check_height)
    result = _depth_fringe_at_radius(depth, objects, indices, mask, x0, y0, physical_seed,
                                     radius=DEPTH_FRINGE_MM, **geometry)
    if result is None:
        result = _depth_fringe_at_radius(depth, objects, indices, mask, x0, y0, physical_seed,
                                         radius=NARROW_DEPTH_FRINGE_MM, **geometry)
    if result is None:
        return np.array([], dtype=int), np.array([], dtype=int), np.empty((0, 3))
    return result


def _depth_fringe_at_radius(depth, objects, indices, mask, x0, y0, physical_seed, *,
                           radius, transform, translation, intrinsics, table_z, check_height):
    """Return the whole supported outline; only area-budget rejection returns None."""
    empty = (np.array([], dtype=int), np.array([], dtype=int), np.empty((0, 3)))
    raised = physical_seed[physical_seed[:, 2] > table_z + 30.0]
    if len(raised) < MIN_CUP_PIXELS:
        return empty
    # Low rim/body pixels still describe the cup's XY footprint. Use the whole
    # validated mask for that envelope, but only raised pixels seed growth.
    hull = cv2.convexHull(physical_seed[:, :2].astype(np.float32))
    if cv2.contourArea(hull) < 1:
        return empty
    min_scale = float(np.linalg.svd(transform, compute_uv=False).min())
    original_depth = depth[y0:y0+mask.shape[0], x0:x0+mask.shape[1]][mask]
    positive = original_depth[np.isfinite(original_depth) & (original_depth > 0)]
    pad = int(np.ceil(radius * max(intrinsics["fx"], intrinsics["fy"])
                      / (min_scale * positive.min()))) + 2
    # Bound processing for unusual near-camera geometry; fall back to exact
    # segmentation instead of widening the attribution arbitrarily.
    if pad > 80:
        return empty
    left, top = max(0, x0-pad), max(0, y0-pad)
    right = min(depth.shape[1], x0+mask.shape[1]+pad)
    bottom = min(depth.shape[0], y0+mask.shape[0]+pad)
    if (bottom-top) * (right-left) > MAX_FRINGE_PIXELS:
        return empty
    body = np.zeros((bottom-top, right-left), dtype=bool)
    body[y0-top:y0-top+mask.shape[0], x0-left:x0-left+mask.shape[1]] = mask
    protected = np.zeros_like(body)
    for index, obj in enumerate(objects):
        if index in indices:
            continue
        region = decode_mask(obj.get("segmentation"), depth.shape)
        if region is None:
            box = _box(obj)
            if box is not None:
                a, b = max(left, int(np.floor(box[0]))), max(top, int(np.floor(box[1])))
                c, d = min(right, int(np.ceil(box[2]))+1), min(bottom, int(np.ceil(box[3]))+1)
                if a < c and b < d:
                    protected[b-top:d-top, a-left:c-left] = True
        else:
            x, y, part = region
            a, b, c, d = max(left, x), max(top, y), min(right, x+part.shape[1]), min(bottom, y+part.shape[0])
            if a < c and b < d:
                protected[b-top:d-top, a-left:c-left] |= part[b-y:d-y, a-x:c-x]
    optical = depth[top:bottom, left:right].astype(float)
    yy, xx = np.indices(body.shape)
    rays = np.stack(((xx+left-intrinsics["cx"])/intrinsics["fx"],
                     (yy+top-intrinsics["cy"])/intrinsics["fy"], np.ones(body.shape)), axis=-1)
    physical = (rays @ transform.T) * optical[..., None] + translation
    height = physical[..., 2] - table_z
    usable = (np.isfinite(optical) & (optical > 0) & np.isfinite(physical).all(axis=-1)
              & (height > 30.0) & (height <= check_height) & ~protected)
    seed = body & usable
    if seed.sum() < MIN_CUP_PIXELS:
        return empty
    _, background = cv2.connectedComponents((~body).astype(np.uint8), connectivity=8)
    outside = np.unique(np.concatenate((background[0], background[-1], background[:, 0], background[:, -1])))
    exterior = np.isin(background, outside[outside != 0])
    distance = cv2.distanceTransform((~body).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    nearby = distance * optical / max(intrinsics["fx"], intrinsics["fy"]) * min_scale <= radius
    candidate = usable & exterior & nearby
    candidate &= ((physical[..., 2] >= raised[:, 2].min()-5.0)
                  & (physical[..., 2] <= raised[:, 2].max()+5.0))
    ys, xs = np.nonzero(candidate)
    for y, x in zip(ys, xs):
        if cv2.pointPolygonTest(hull, tuple(map(float, physical[y, x, :2])), True) < -radius:
            candidate[y, x] = False
    allowed = seed | candidate
    reached, frontier = seed.copy(), seed.copy()
    edges = []
    h, w = body.shape
    for dy, dx in ((-1,-1), (-1,0), (-1,1), (0,-1), (0,1), (1,-1), (1,0), (1,1)):
        source = (slice(max(0,-dy), min(h,h-dy)), slice(max(0,-dx), min(w,w-dx)))
        target = (slice(max(0,dy), min(h,h+dy)), slice(max(0,dx), min(w,w+dx)))
        continuity = np.sum((physical[target]-physical[source]) ** 2, axis=-1) <= MAX_SURFACE_STEP_MM ** 2
        edges.append((source, target, continuity))
    for _ in range(h+w):
        following = np.zeros_like(body)
        for source, target, continuity in edges:
            following[target] |= frontier[source] & allowed[target] & continuity
        following &= ~reached
        if not following.any():
            break
        reached |= following
        frontier = following
    else:
        return empty
    extra = reached & ~body
    if extra.sum() > MAX_FRINGE_FRACTION * mask.sum():
        return None
    ys, xs = np.nonzero(extra)
    return ys+top, xs+left, physical[ys, xs]


def assign_cups(depth, objects, locations, *, transform, translation, intrinsics, cell_size,
                table_z=0.0, check_height=160.0):
    """Return per-pixel location owners, consumed detections and evidence.

    Owners are location-list indices plus one (zero means no exemption; -1
    means conflicting owners). Coordinates use the aligned depth and complete
    calibration, including tag offsets exactly once. A majority of observed
    cup surface pixels must lie in one cell, with a unique winner. This is an
    occupancy assignment, not proof that occluded table pixels are empty.
    """
    if (not isinstance(objects, (list, tuple))
            or any(not isinstance(obj, dict) or not isinstance(obj.get("class"), str)
                   or not obj["class"] for obj in objects)):
        raise SelectionError("Placement detections must be a list of labelled objects. Scan again.")
    ownership = np.zeros(depth.shape, dtype=np.int32)
    decoded, boxes = {}, {}
    for index, obj in enumerate(objects):
        confidence = obj.get("confidence")
        box = _box(obj)
        if (obj.get("class") not in CUP_CLASSES or box is None
                or not finite_number(confidence) or not MIN_CONFIDENCE <= confidence <= 1):
            continue
        region = decode_mask(obj.get("segmentation"), depth.shape)
        if region is None:
            continue
        x, y, mask = region
        ys, xs = np.nonzero(mask)
        # Masks from a different observation/box cannot grant an exemption.
        if (xs.min() + x < box[0] - 2 or xs.max() + x > box[2] + 2
                or ys.min() + y < box[1] - 2 or ys.max() + y > box[3] + 2):
            continue
        decoded[index], boxes[index] = region, box

    # Use every body box for association, even when its own mask is missing.
    # A part shared by two bodies is ambiguous and cannot erase either one.
    bodies = {i: _box(obj) for i, obj in enumerate(objects) if obj.get("class") == "CUP"}
    bodies = {i: box for i, box in bodies.items() if box is not None}
    groups = {i: [i] for i in bodies if i in decoded}
    standalone = []
    for index in decoded:
        obj = objects[index]
        if obj["class"] == "CUP":
            continue
        parents = [i for i, box in bodies.items()
                   if _inside_part(boxes[index], box, handle=obj["class"] == "CUP HANDLE")]
        if len(parents) == 1 and parents[0] in groups:
            groups[parents[0]].append(index)
        elif not parents and obj["class"] == "CUP BASE" and obj.get("is_cup") is True:
            standalone.append(index)

    # Overlapping duplicate base predictions without a CUP body are one cup.
    # Separate neighbouring bases are never merged merely because boxes touch.
    for index in standalone:
        x, y, mask = decoded[index]
        matches = []
        for root in standalone:
            if root not in groups:
                continue
            bx, by, other = decoded[root]
            x0, y0 = max(x, bx), max(y, by)
            x1, y1 = min(x + mask.shape[1], bx + other.shape[1]), min(y + mask.shape[0], by + other.shape[0])
            if x0 >= x1 or y0 >= y1:
                continue
            overlap = (mask[y0-y:y1-y, x0-x:x1-x] & other[y0-by:y1-by, x0-bx:x1-bx]).sum()
            if overlap / min(mask.sum(), other.sum()) >= 0.7:
                matches.append(root)
        if len(matches) == 1:
            groups[matches[0]].append(index)
        elif not matches:
            groups[index] = [index]

    consumed, assignments = set(), []
    half = cell_size / 2
    for indices in groups.values():
        x0 = min(decoded[i][0] for i in indices)
        y0 = min(decoded[i][1] for i in indices)
        x1 = max(decoded[i][0] + decoded[i][2].shape[1] for i in indices)
        y1 = max(decoded[i][1] + decoded[i][2].shape[0] for i in indices)
        mask = np.zeros((y1-y0, x1-x0), dtype=bool)
        for i in indices:
            x, y, part = decoded[i]
            mask[y-y0:y-y0+part.shape[0], x-x0:x-x0+part.shape[1]] |= part
        ys, xs = np.nonzero(mask)
        if len(xs) < MIN_CUP_PIXELS:
            continue
        optical_z = depth[y0+ys, x0+xs].astype(float)
        valid = np.isfinite(optical_z) & (optical_z > 0)
        if valid.sum() < MIN_CUP_PIXELS or valid.mean() < MIN_USABLE_DEPTH:
            continue
        rays = np.column_stack(((x0+xs[valid]-intrinsics["cx"])/intrinsics["fx"],
                                (y0+ys[valid]-intrinsics["cy"])/intrinsics["fy"],
                                np.ones(valid.sum())))
        physical = (rays @ transform.T) * optical_z[valid, None] + translation
        plausible = (np.isfinite(physical).all(axis=1)
                     & (physical[:, 2] >= table_z - 30.0)
                     & (physical[:, 2] <= table_z + check_height))
        if plausible.sum() / len(xs) < MIN_USABLE_DEPTH:
            continue
        # The segmented cup determines its cell. A recovered depth outline is
        # measurement uncertainty, so it must not move the cup's majority to
        # another cell or break a tie between cells.
        xy = physical[plausible, :2]
        scores = np.array([np.count_nonzero((np.abs(xy[:, 0] - place["x"]) <= half)
                                            & (np.abs(xy[:, 1] - place["y"]) <= half))
                           for place in locations])
        if not len(scores) or scores.max() / len(xy) < MIN_DOMINANT_FRACTION:
            continue
        winners = np.flatnonzero(scores == scores.max())
        if len(winners) != 1:
            continue
        winner = int(winners[0])
        owner = winner + 1
        extra_y, extra_x, _ = _depth_fringe(
            depth, objects, indices, mask, x0, y0, physical[plausible], transform=transform,
            translation=translation, intrinsics=intrinsics, table_z=table_z, check_height=check_height)
        # A mostly usable mask may still contain depth holes or implausibly
        # high/low samples. Those pixels retain the normal obstacle/unknown
        # checks rather than inheriting the rest of the cup's exemption.
        supported = np.zeros_like(mask)
        supported[ys[valid][plausible], xs[valid][plausible]] = True
        region = ownership[y0:y1, x0:x1]
        conflict = supported & (region != 0) & (region != owner)
        region[supported & (region == 0)] = owner
        region[conflict] = -1
        previous = ownership[extra_y, extra_x]
        ownership[extra_y, extra_x] = np.where((previous == 0) | (previous == owner), owner, -1)
        consumed.update(indices)
        assignments.append({"location_id": locations[winner]["id"], "object_indices": list(indices),
                            "detection_ids": [objects[i].get("detection_id", i) for i in indices],
                            "overlap_fraction": round(float(scores[winner] / len(xy)), 4),
                            "center_mm": np.median(xy, axis=0).round(2).tolist(),
                            "mask_pixels": int(mask.sum()), "depth_fringe_pixels": len(extra_y)})
    return ownership, consumed, assignments
