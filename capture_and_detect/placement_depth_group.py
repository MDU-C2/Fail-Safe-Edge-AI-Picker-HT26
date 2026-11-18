"""Bounded shared attribution when depth joins several identified cups.

The operator accepts that a small unlabelled object touching a cup can be
missed. This is an attribution heuristic, not a correction to measured depth.
Callers must first protect other labelled objects, mask holes, and conflicting
ownership, and establish each cup's physical cell from its exact raised seeds.
Only observed pixels are returned; the owning cells still contain their cups.
"""

import cv2
import numpy as np

from cup_inventory import finite_number


MAX_REGION_PIXELS = 131072
MIN_EXCLUSIVE_SEEDS = 32
MIN_SUPPORT_FRACTION = .5
XY_BIN_MM = 2.
CUP_EDGE_TOLERANCE_MM = 20.


def shared_group_region(component, seeds, seed_hulls, points, optical_depth, eligible,
                        *, locations, owner_indices, cell_size, min_depth_scale, focal_length):
    """Return an observed shared mask and audit metadata, or ``None``.

    Two to four cups must jointly cover half the component with exclusive exact
    seeds, with at least 32 per cup. Half its unique 2 mm physical XY bins must
    lie within the union of their cells. A sample physically inside another
    cell needs both image and XY proximity within 20 mm of the SAME cup.
    Full exact-mask hulls and distance seeds must contain only valid observed
    support, prepared by the caller; invalid or missing depth is never shared.

    This deliberately pools cup support when the stricter individual partition
    test fails. It does not impose that test's individual coverage, surface-step
    or fringe-area limits. Existing owners, exact masks and conflicts remain
    untouched. The caller removes shared pixels from visible-area accounting.
    """
    if (not isinstance(component, np.ndarray) or component.ndim != 2
            or component.dtype != np.bool_ or not 0 < component.size <= MAX_REGION_PIXELS
            or not isinstance(seeds, (list, tuple)) or not 2 <= len(seeds) <= 4
            or not isinstance(seed_hulls, (list, tuple)) or len(seed_hulls) != len(seeds)
            or not isinstance(eligible, np.ndarray) or eligible.dtype != np.bool_
            or eligible.shape != component.shape
            or not isinstance(points, np.ndarray) or points.shape != component.shape + (3,)
            or not np.issubdtype(points.dtype, np.number) or np.iscomplexobj(points)
            or not isinstance(optical_depth, np.ndarray) or optical_depth.shape != component.shape
            or not np.issubdtype(optical_depth.dtype, np.number) or np.iscomplexobj(optical_depth)
            or not isinstance(locations, (list, tuple)) or not locations
            or not isinstance(owner_indices, (list, tuple)) or len(owner_indices) != len(seeds)
            or any(type(i) is not int or not 0 <= i < len(locations) for i in owner_indices)
            or len(set(owner_indices)) != len(owner_indices)
            or not all(finite_number(v) for v in (cell_size, min_depth_scale, focal_length))
            or not 20 <= cell_size <= 200 or min_depth_scale <= 1e-12 or focal_length <= 0
            or any(not isinstance(p, dict) or not all(finite_number(p.get(k)) for k in ("x", "y"))
                   for p in locations)):
        return None
    count = np.zeros(component.shape, dtype=np.uint8)
    hulls = []
    for seed, hull in zip(seeds, seed_hulls):
        if (not isinstance(seed, np.ndarray) or seed.shape != component.shape
                or seed.dtype != np.bool_ or not isinstance(hull, np.ndarray)
                or hull.ndim != 3 or hull.shape[1:] != (1, 2) or len(hull) < 3
                or not np.issubdtype(hull.dtype, np.number) or np.iscomplexobj(hull)
                or not np.isfinite(hull).all() or np.any(np.abs(hull) > 2**50)):
            return None
        hull = hull.astype(np.float32)
        if cv2.contourArea(hull) <= 0:
            return None
        hulls.append(hull)
        count += seed
    if not component.any():
        return None
    exclusive = component & (count == 1)
    if (any(np.count_nonzero(exclusive & seed) < MIN_EXCLUSIVE_SEEDS for seed in seeds)
            or exclusive.sum() < component.sum() * MIN_SUPPORT_FRACTION):
        return None
    support = component | (count > 0)
    if (not np.isfinite(points[support]).all() or np.any(np.abs(points[support, :2]) > 2**50)
            or not np.isfinite(optical_depth[support]).all() or np.any(optical_depth[support] <= 0)):
        return None
    rows, columns = np.nonzero(component)
    world = points[rows, columns]
    bins = np.unique(np.floor(world[:, :2] / XY_BIN_MM).astype(np.int64), axis=0)
    centers = (bins + .5) * XY_BIN_MM
    union = np.zeros(len(bins), dtype=bool)
    half = cell_size / 2
    foreign = np.zeros(len(rows), dtype=bool)
    for i, place in enumerate(locations):
        if i in owner_indices:
            union |= ((np.abs(centers[:, 0] - place["x"]) <= half)
                      & (np.abs(centers[:, 1] - place["y"]) <= half))
        else:
            foreign |= ((np.abs(world[:, 0] - place["x"]) <= half)
                        & (np.abs(world[:, 1] - place["y"]) <= half))
    if union.sum() < len(bins) * MIN_SUPPORT_FRACTION:
        return None
    usable = eligible[rows, columns] & (count[rows, columns] == 0)
    supported = np.zeros(len(rows), dtype=bool)
    for seed, hull in zip(seeds, hulls):
        distance = cv2.distanceTransform((~seed).astype(np.uint8), cv2.DIST_L2,
                                         cv2.DIST_MASK_PRECISE)[rows, columns]
        with np.errstate(over="ignore", invalid="ignore"):
            image_distance = distance * optical_depth[rows, columns] / focal_length * min_depth_scale
        near = usable & foreign & ~supported & (image_distance <= CUP_EDGE_TOLERANCE_MM)
        for index in np.flatnonzero(near):
            if cv2.pointPolygonTest(hull, tuple(map(float, world[index, :2])), True) >= -CUP_EDGE_TOLERANCE_MM:
                supported[index] = True
    selected = np.zeros(component.shape, dtype=bool)
    selected[rows, columns] = usable & (~foreign | supported)
    if not selected.any():
        return None
    metadata = {
        "component_pixels": int(component.sum()),
        "exclusive_seed_fraction": float(exclusive.sum() / component.sum()),
        "owner_area_fraction": float(union.sum() / len(bins)),
        "shared_pixels": int(selected.sum()),
        "foreign_cell_pixels": int((usable & foreign).sum()),
        "foreign_cell_exempted_pixels": int((usable & foreign & supported).sum()),
        "foreign_cell_retained_pixels": int((usable & foreign & ~supported).sum()),
        "edge_tolerance_mm": CUP_EDGE_TOLERANCE_MM,
    }
    return selected, metadata
