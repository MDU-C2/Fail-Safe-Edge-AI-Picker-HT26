"""Attribute raised depth to known cups, including narrowly shared boundaries.

This deliberately accepts an ambiguity: an unlabelled object touching a cup's
depth region can be attributed to that cup. Connectivity and dominant physical
area cannot establish object identity. Separate regions, other labelled objects,
unresolved cup associations, mask holes and unsupported depth remain obstacles.
Only observed samples are attributed; hidden pixels are never reconstructed.
There is no file, network or hardware access.
"""

import copy

import cv2
import numpy as np

from cup_inventory import finite_number
from detection_masks import decode_mask
from placement_depth_split import MAX_GROUPS, split_component
from placement_depth_shared import shared_boundaries
from placement_depth_group import shared_group_region


MAX_FRAME_PIXELS = 8_388_608
MAX_REGION_PIXELS = 131072
MAX_OBJECTS = 128
MIN_SEED_PIXELS = 32
MIN_SEED_FRACTION = 0.5
MIN_DOMINANT_FRACTION = 0.5
XY_BIN_MM = 2.0
_METADATA = ("depth_region_pixels", "depth_region_extra_pixels", "depth_area_mm2",
             "depth_area_by_location_mm2", "depth_area_fraction", "depth_assignment_method",
             "depth_seed_area_mm2", "depth_seed_area_fraction", "depth_split_components",
             "depth_split_pixels", "depth_split_seam_pixels", "depth_shared_boundary_pixels",
             "depth_shared_groups")


def _holes(mask):
    """Return enclosed background, using the same eight-neighbour connectivity."""
    background = np.pad((~mask).astype(np.uint8), 1, constant_values=1)
    cv2.floodFill(background, None, (0, 0), 2, flags=8)
    return background[1:-1, 1:-1] == 1


def _box_region(obj, shape):
    box = obj.get("bbox_xyxy")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(finite_number(value) for value in box)
            or box[2] <= box[0] or box[3] <= box[1]):
        return None
    height, width = shape
    x0, y0 = max(0, int(np.floor(box[0]))), max(0, int(np.floor(box[1])))
    x1, y1 = min(width, int(np.ceil(box[2])) + 1), min(height, int(np.ceil(box[3])) + 1)
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def assign_depth_regions(depth, objects, locations, owners, assignments, *, transform,
                         translation, intrinsics, cell_size, table_z=0., check_height=160.):
    """Return ownership, assignment copies and explicit shared cup boundaries.

    Each accepted component or split part has at least 32 exact cup-mask seed
    pixels, at least 50% seed coverage, and no other labelled object. A component
    shared by 2--4 cups in distinct cells may be split by their exclusive masks;
    every part must pass before any split ownership is applied. Components
    are pooled per cup, then unique 2 mm physical XY bins vote for one cell.
    The exact raised cup-mask footprint must have a unique majority cell,
    including samples outside the grid in its denominator. The pooled region's
    unique largest-area cell must agree with that cell and the original cup
    assignment. Depth spill outside grid cells cannot dilute this second vote.

    ``depth_area_mm2`` is the full sampled XY-bin area, not inferred hidden area.
    ``depth_region_pixels`` counts accepted region pixels actually owned by this
    cup after conflicts; ``depth_region_extra_pixels`` counts only zero-to-owner
    changes. Existing -1 conflicts are preserved. Accepted splits mark their
    non-exact uncertain seam -1, including pixels previously owned by a fringe.
    A narrowly supported boundary may additionally belong to two known cups;
    its coordinates and both owner indices are returned separately. It never
    receives an invented single owner. Both owner cells remain occupied.
    Bounds also limit mask/candidate bounding-box work and the pooled pixels of
    each cup. Rejection retains exact-mask/fringe ownership without augmentation.

    If a 2--4 cup split cannot pass individually, the operator-selected group
    fallback may share their observed region: each cup still needs its exact
    physical cell vote, but coverage and area are pooled across the group.
    Samples inside another cell require both image and physical XY proximity
    within 20 mm of a valid exact cup outline. Small unlabelled touching objects
    can be missed by this explicitly accepted tolerance. See ``depth_shared_groups``
    for its audit evidence; no individual cup is assigned the pooled footprint.
    """
    if (not isinstance(depth, np.ndarray) or depth.ndim != 2
            or not np.issubdtype(depth.dtype, np.number) or np.iscomplexobj(depth)
            or not isinstance(owners, np.ndarray) or owners.shape != depth.shape
            or not np.issubdtype(owners.dtype, np.signedinteger)
            or not 0 < depth.size <= MAX_FRAME_PIXELS
            or not isinstance(objects, (list, tuple)) or len(objects) > MAX_OBJECTS
            or any(not isinstance(obj, dict) or not isinstance(obj.get("class"), str) for obj in objects)
            or not isinstance(assignments, (list, tuple)) or len(assignments) > MAX_OBJECTS
            or any(not isinstance(group, dict) for group in assignments)
            or not isinstance(locations, (list, tuple)) or not locations):
        return owners, assignments, []
    updated = owners.copy()
    records = copy.deepcopy(assignments)
    for record in records:
        for key in _METADATA:
            record.pop(key, None)
    fallback = (updated, records, [])
    if not records:
        return fallback
    try:
        matrix, origin = np.asarray(transform, dtype=float), np.asarray(translation, dtype=float)
        fx, fy, cx, cy = (intrinsics[key] for key in ("fx", "fy", "cx", "cy"))
        if (matrix.shape != (3, 3) or origin.shape != (3,)
                or not np.isfinite(matrix).all() or not np.isfinite(origin).all()
                or not all(finite_number(value) for value in (fx, fy, cx, cy, cell_size, table_z, check_height))
                or fx <= 0 or fy <= 0 or not 20 <= cell_size <= 200 or check_height <= 30
                or any(not isinstance(place, dict) or type(place.get("id")) is not int
                       or not all(finite_number(place.get(axis)) for axis in ("x", "y")) for place in locations)):
            return fallback
    except (KeyError, TypeError, ValueError):
        return fallback
    place_indices = {place["id"]: index for index, place in enumerate(locations)}
    if len(place_indices) != len(locations):
        return fallback
    height, width = depth.shape
    work_limit = 4 * MAX_FRAME_PIXELS
    decoded, work = [], 0
    for obj in objects:
        region = decode_mask(obj.get("segmentation"), depth.shape)
        if region is not None:
            work += region[2].size
            if work > work_limit:
                return fallback
        decoded.append(region)
    groups = []
    forbidden_holes = np.zeros(depth.shape, dtype=bool)
    for record_index, record in enumerate(records):
        indices = record.get("object_indices")
        if (type(record.get("location_id")) is not int or record["location_id"] not in place_indices
                or not isinstance(indices, (list, tuple)) or not indices
                or any(type(index) is not int or not 0 <= index < len(objects) for index in indices)
                or len(set(indices)) != len(indices) or any(decoded[index] is None for index in indices)):
            continue
        x0, y0 = min(decoded[i][0] for i in indices), min(decoded[i][1] for i in indices)
        x1 = max(decoded[i][0] + decoded[i][2].shape[1] for i in indices)
        y1 = max(decoded[i][1] + decoded[i][2].shape[0] for i in indices)
        if (x1-x0) * (y1-y0) > MAX_REGION_PIXELS:
            continue
        work += (x1-x0) * (y1-y0)
        if work > work_limit:
            return fallback
        mask = np.zeros((y1-y0, x1-x0), dtype=bool)
        for index in indices:
            x, y, part = decoded[index]
            mask[y-y0:y-y0+part.shape[0], x-x0:x-x0+part.shape[1]] |= part
        forbidden_holes[y0:y1, x0:x1] |= _holes(mask)
        groups.append((record_index, set(indices), x0, y0, mask))
    if not groups:
        return fallback
    with np.errstate(over="ignore", invalid="ignore"):
        x_term = matrix[2, 0] * (np.arange(width) - cx) / fx
        y_term = matrix[2, 1] * (np.arange(height) - cy) / fy
        relative_z = (x_term[None, :] + y_term[:, None] + matrix[2, 2]) * depth + origin[2] - table_z
    raised = (np.isfinite(depth) & (depth > 0) & np.isfinite(relative_z)
              & (relative_z > 30.) & (relative_z <= check_height) & ~forbidden_holes)
    del relative_z, forbidden_holes
    _, labels, stats, _ = cv2.connectedComponentsWithStats(raised.astype(np.uint8), connectivity=8)
    del raised
    associations = {}
    for group_index, (_, _, x, y, mask) in enumerate(groups):
        ids, sizes = np.unique(labels[y:y+mask.shape[0], x:x+mask.shape[1]][mask], return_counts=True)
        for component, size in zip(ids, sizes):
            if component:
                associations.setdefault(int(component), []).append((group_index, int(size)))
    area = stats[:, cv2.CC_STAT_AREA]
    bounded = ((area <= MAX_REGION_PIXELS)
               & (stats[:, cv2.CC_STAT_WIDTH].astype(np.int64) * stats[:, cv2.CC_STAT_HEIGHT] <= MAX_REGION_PIXELS)
               & (stats[:, cv2.CC_STAT_LEFT] > 0) & (stats[:, cv2.CC_STAT_TOP] > 0)
               & (stats[:, cv2.CC_STAT_LEFT] + stats[:, cv2.CC_STAT_WIDTH] < width)
               & (stats[:, cv2.CC_STAT_TOP] + stats[:, cv2.CC_STAT_HEIGHT] < height))
    candidates = {}
    for component, members in associations.items():
        if (not bounded[component] or len(members) > MAX_GROUPS
                or any(size < MIN_SEED_PIXELS for _, size in members)):
            continue
        if len(members) == 1:
            if members[0][1] < area[component] * MIN_SEED_FRACTION:
                continue
        elif len({records[groups[g][0]]["location_id"] for g, _ in members}) != len(members):
            continue  # Two cups in one cell cannot jointly claim an ambiguous bridge.
        candidates[component] = set().union(*(groups[g][1] for g, _ in members))
    # Other labelled detections veto the whole component. Missing/invalid masks
    # use a clipped box for protection only; boxes never grant ownership.
    for index, (obj, region) in enumerate(zip(objects, decoded)):
        if region is not None:
            x, y, mask = region
            hit = labels[y:y+mask.shape[0], x:x+mask.shape[1]][mask]
        else:
            box = _box_region(obj, depth.shape)
            if box is None:
                return fallback  # An unlocatable labelled object cannot be protected.
            x0, y0, x1, y1 = box
            work += (x1-x0) * (y1-y0)
            if work > work_limit:
                return fallback
            hit = labels[y0:y1, x0:x1].reshape(-1)
        for component in np.unique(hit):
            if component in candidates and index not in candidates[component]:
                del candidates[component]
    # Check the remaining crop work before mutating any cup's ownership. A
    # later resource rejection must not return a partially augmented fallback.
    work += sum(int(stats[c, cv2.CC_STAT_WIDTH]) * int(stats[c, cv2.CC_STAT_HEIGHT])
                * (1 if len(associations[c]) == 1 else 8 + 12 * len(associations[c]))
                for c in candidates)
    if work > work_limit:
        return fallback
    half = cell_size / 2

    def physical_points(rows, columns):
        optical = depth[rows, columns].astype(float)
        rays = np.column_stack(((columns-cx)/fx, (rows-cy)/fy, np.ones(len(rows))))
        with np.errstate(over="ignore", invalid="ignore"):
            points = (rays @ matrix.T) * optical[:, None] + origin
        if not np.isfinite(points).all() or np.any(np.abs(points[:, :2]) > 2**50):
            return None
        return points

    def physical_bins(rows, columns):
        points = physical_points(rows, columns)
        if points is None:
            return None
        return np.unique(np.floor(points[:, :2] / XY_BIN_MM).astype(np.int64), axis=0)

    def bin_scores(bins):
        centers = (bins + .5) * XY_BIN_MM
        return np.array([np.count_nonzero((np.abs(centers[:, 0] - place["x"]) <= half)
                                          & (np.abs(centers[:, 1] - place["y"]) <= half))
                         for place in locations])

    def area_votes(rows, columns):
        bins = physical_bins(rows, columns)
        return None if bins is None else (len(bins), bin_scores(bins))

    def shared_vote_agrees(part, boundary, location_id):
        """Owner must win even if only adverse boundary bins belong to it.

        Giving the owner all boundary samples can conceal a competing cell.
        Instead, each competitor receives every possible new bin and the owner
        receives no beneficial credit. Duplicate physical bins count once.
        """
        base = physical_bins(*part)
        extra = physical_bins(*boundary)
        if base is None or extra is None or not len(base):
            return False
        combined = np.unique(np.concatenate((base, extra)), axis=0)
        scores = bin_scores(combined)
        winner = place_indices[location_id]
        scores[winner] = bin_scores(base)[winner]
        return scores[winner] > 0 and np.all(scores[winner] > np.delete(scores, winner))

    def agreeing_vote(vote, location_id, *, majority=False):
        if vote is None or not vote[0]:
            return False
        bins, scores = vote
        winners = np.flatnonzero(scores == scores.max())
        return (len(winners) == 1 and scores[winners[0]] > 0
                and locations[int(winners[0])]["id"] == location_id
                and (not majority or scores[winners[0]] >= bins * MIN_DOMINANT_FRACTION))

    seed_votes = {}
    needed = {g for c in candidates for g, _ in associations[c]}
    for group_index in needed:
        record_index, _, seed_x, seed_y, mask = groups[group_index]
        # Establish the owner from observed exact cup seeds, including all raised
        # parts of its mask rather than just the components accepted below.
        seed_rows, seed_columns = np.nonzero(
            mask & (labels[seed_y:seed_y+mask.shape[0], seed_x:seed_x+mask.shape[1]] > 0))
        seed_vote = area_votes(seed_rows + seed_y, seed_columns + seed_x)
        if agreeing_vote(seed_vote, records[record_index]["location_id"], majority=True):
            seed_votes[group_index] = seed_vote

    # Build complete bundles before touching ownership. A shared component is
    # accepted atomically, even if a participant fails the later pooled vote.
    bundles = []
    for component in candidates:
        members = [g for g, _ in associations[component]]
        if any(g not in seed_votes for g in members):
            continue
        x, y, w, h = stats[component, :4]
        region = labels[y:y+h, x:x+w] == component
        seam = None
        shared = []
        if len(members) == 1:
            partitions = [region]
        else:
            seeds = []
            for g in members:
                _, _, sx, sy, mask = groups[g]
                seed = np.zeros(region.shape, dtype=bool)
                left, top = max(x, sx), max(y, sy)
                right, bottom = min(x+w, sx+mask.shape[1]), min(y+h, sy+mask.shape[0])
                seed[top-y:bottom-y, left-x:right-x] = mask[top-sy:bottom-sy, left-sx:right-sx]
                seeds.append(seed & region)
            result = split_component(region, seeds)
            if result is None:
                continue
            partitions, seam = result
        parts = {}
        for g, partition in zip(members, partitions):
            rows, columns = np.nonzero(partition)
            rows, columns = rows+y, columns+x
            # A split part must agree before it can be pooled with other
            # components. Pooling must never rescue an unsupported split part.
            if len(members) > 1 and not agreeing_vote(
                    area_votes(rows, columns), records[groups[g][0]]["location_id"]):
                break
            parts[g] = (rows, columns)
        else:
            seam_coordinates = None
            if seam is not None:
                rows, columns = np.nonzero(seam)
                seam_coordinates = (rows+y, columns+x)
                crop_rows, crop_columns = np.indices(region.shape)
                world = physical_points((crop_rows+y).ravel(), (crop_columns+x).ravel())
                if world is not None:
                    original_crop = owners[y:y+h, x:x+w].copy()
                    for g, partition in zip(members, partitions):
                        owner = place_indices[records[groups[g][0]]["location_id"]] + 1
                        original_crop[partition & (original_crop > 0) & (original_crop != owner)] = -1
                    for a, b, mask in shared_boundaries(
                            region, seeds, partitions, seam, original_crop,
                            world.reshape(region.shape + (3,))):
                        rows, columns = np.nonzero(mask)
                        shared.append((frozenset((members[a], members[b])), (rows+y, columns+x)))
                # Every possible boundary sample is charged against each cup's
                # seed coverage. Area voting uses only adverse new bins.
                for g, partition, seed in zip(members, partitions, seeds):
                    associated = [coords for pair, coords in shared if g in pair]
                    if not associated:
                        continue
                    boundary = tuple(np.concatenate([c[i] for c in associated]) for i in (0, 1))
                    if (seed.sum() < (partition.sum() + len(boundary[0])) * MIN_SEED_FRACTION
                            or not shared_vote_agrees(parts[g], boundary, records[groups[g][0]]["location_id"])):
                        shared = []
                        break
            bundles.append({"component": component, "parts": parts, "seam": seam_coordinates, "shared": shared})

    def pooled_plans(active):
        coordinates = {}
        for bundle in active:
            for g, part in bundle["parts"].items():
                coordinates.setdefault(g, []).append(part)
        plans = {}
        for g, parts in coordinates.items():
            if sum(len(part[0]) for part in parts) > MAX_REGION_PIXELS:
                continue
            rows = np.concatenate([part[0] for part in parts])
            columns = np.concatenate([part[1] for part in parts])
            vote = area_votes(rows, columns)
            if agreeing_vote(vote, records[groups[g][0]]["location_id"]):
                plans[g] = (rows, columns, vote)
        return plans, set(coordinates) - set(plans)

    plans, failed = pooled_plans(bundles)
    # Reject the whole chain of shared bundles connected to any failed cup.
    # Unrelated single-cup evidence remains eligible. One recomputation suffices:
    # surviving shared bundles have exactly the same participants and pools.
    rejected = set(failed)
    while True:
        expanded = rejected.union(*(set(b["parts"]) for b in bundles
                                    if b["seam"] is not None and rejected.intersection(b["parts"])))
        if expanded == rejected:
            break
        rejected = expanded
    if rejected:
        bundles = [b for b in bundles if b["seam"] is None or not rejected.intersection(b["parts"])]
        plans, _ = pooled_plans(bundles)
    # Check all possible shared samples together against the final pooled cup
    # footprint. Removing rejected boundaries cannot weaken this adverse test.
    shared_by_group = {}
    for bundle in bundles:
        for pair, coordinates in bundle["shared"]:
            for g in pair:
                shared_by_group.setdefault(g, []).append(coordinates)
    rejected_shared = set()
    for g, coordinates in shared_by_group.items():
        boundary = tuple(np.concatenate([c[i] for c in coordinates]) for i in (0, 1))
        if (g not in plans or len(plans[g][0]) + len(boundary[0]) > MAX_REGION_PIXELS
                or not shared_vote_agrees(plans[g][:2], boundary, records[groups[g][0]]["location_id"])):
            rejected_shared.add(g)
    shared_regions = []
    for bundle in bundles:
        if any(rejected_shared.intersection(pair) for pair, _ in bundle["shared"]):
            bundle["shared"] = []
        for pair, coordinates in bundle["shared"]:
            shared_regions.append((frozenset(place_indices[records[groups[g][0]]["location_id"]] + 1
                                             for g in pair), coordinates))
        if bundle["seam"] is not None:
            updated[bundle["seam"]] = -1
    for g, (rows, columns, (region_bins, scores)) in plans.items():
        record = records[groups[g][0]]
        winner = place_indices[record["location_id"]]
        owner = winner + 1
        seed_bins, seed_scores = seed_votes[g]
        previous = updated[rows, columns]
        extra = int(np.count_nonzero(previous == 0))
        updated[rows, columns] = np.where((previous == 0) | (previous == owner), owner, -1)
        record.update(depth_assignment_method="connected_depth_area",
                      depth_region_pixels=int(np.count_nonzero(updated[rows, columns] == owner)),
                      depth_region_extra_pixels=extra,
                      depth_area_mm2=float(region_bins * XY_BIN_MM ** 2),
                      depth_area_by_location_mm2=[{"location_id": place["id"],
                          "area_mm2": float(scores[i] * XY_BIN_MM ** 2)} for i, place in enumerate(locations)],
                      depth_area_fraction=float(scores[winner] / region_bins),
                      depth_seed_area_mm2=float(seed_bins * XY_BIN_MM ** 2),
                      depth_seed_area_fraction=float(seed_scores[winner] / seed_bins))
        split_bundles = [b for b in bundles if b["seam"] is not None and g in b["parts"]]
        if split_bundles:
            record.update(depth_split_components=len(split_bundles),
                          depth_split_pixels=sum(len(b["parts"][g][0]) for b in split_bundles),
                          depth_split_seam_pixels=sum(len(b["seam"][0]) for b in split_bundles),
                          depth_shared_boundary_pixels=sum(len(c[0]) for b in split_bundles
                                                           for pair, c in b["shared"] if g in pair))
    # A failed individual split may still have reliable collective cup support.
    # Keep that ambiguity explicit; never invent one owner for these samples.
    accepted = {bundle["component"] for bundle in bundles}
    support_cache = {}
    # Cap augmentation work per cup (accepted region plans plus shared groups).
    # The original exact-mask/fringe ownership remains independently bounded.
    shared_counts = {g: len(plan[0]) for g, plan in plans.items()}
    for bundle in bundles:
        for pair, coordinates in bundle["shared"]:
            for g in pair:
                shared_counts[g] = shared_counts.get(g, 0) + len(coordinates[0])
    scale = float(np.linalg.svd(matrix, compute_uv=False).min())
    if not np.isfinite(scale) or scale <= 1e-12:
        return updated, records, shared_regions
    for component in candidates:
        members = [g for g, _ in associations[component]]
        if (component in accepted or not 2 <= len(members) <= MAX_GROUPS
                or any(g not in seed_votes for g in members)):
            continue
        x, y, w, h = stats[component, :4]
        region = labels[y:y+h, x:x+w] == component
        seeds, hulls = [], []
        for g in members:
            record_index, _, sx, sy, mask = groups[g]
            if g not in support_cache:
                local_depth = depth[sy:sy+mask.shape[0], sx:sx+mask.shape[1]]
                my, mx = np.nonzero(mask & np.isfinite(local_depth) & (local_depth > 0))
                world = physical_points(my+sy, mx+sx)
                support_cache[g] = None
                if world is not None:
                    optical = depth[my+sy, mx+sx]
                    owner = place_indices[records[record_index]["location_id"]] + 1
                    valid = (np.isfinite(optical) & (optical > 0)
                             & (world[:, 2]-table_z >= -30.) & (world[:, 2]-table_z <= check_height)
                             & (owners[my+sy, mx+sx] == owner))
                    if np.count_nonzero(valid) >= MIN_SEED_PIXELS:
                        support = np.zeros(mask.shape, dtype=bool)
                        support[my[valid], mx[valid]] = True
                        hull = cv2.convexHull(world[valid, :2].astype(np.float32))
                        if len(hull) >= 3 and cv2.contourArea(hull) > 0:
                            support_cache[g] = support, hull
            cached = support_cache[g]
            if cached is None:
                break
            support, hull = cached
            seed = np.zeros(region.shape, dtype=bool)
            left, top = max(x, sx), max(y, sy)
            right, bottom = min(x+w, sx+mask.shape[1]), min(y+h, sy+mask.shape[0])
            seed[top-y:bottom-y, left-x:right-x] = support[top-sy:bottom-sy, left-sx:right-sx]
            seeds.append(seed)
            hulls.append(hull)
        else:
            supported_pixels = region | np.logical_or.reduce(seeds)
            rows, columns = np.nonzero(supported_pixels)
            world = physical_points(rows+y, columns+x)
            if world is None:
                continue
            points = np.full(region.shape + (3,), np.nan)
            points[rows, columns] = world
            owner_indices = [place_indices[records[groups[g][0]]["location_id"]] for g in members]
            eligible = ((owners[y:y+h, x:x+w] == 0) & (updated[y:y+h, x:x+w] == 0))
            result = shared_group_region(
                region, seeds, hulls, points,
                depth[y:y+h, x:x+w], eligible,
                locations=locations, owner_indices=owner_indices, cell_size=cell_size,
                min_depth_scale=scale, focal_length=max(fx, fy))
            if result is None:
                continue
            selected, evidence = result
            if any(shared_counts.get(g, 0) + evidence["shared_pixels"] > MAX_REGION_PIXELS for g in members):
                continue
            rows, columns = np.nonzero(selected)
            shared_regions.append((frozenset(i+1 for i in owner_indices), (rows+y, columns+x)))
            evidence["location_ids"] = [locations[i]["id"] for i in owner_indices]
            for g in members:
                shared_counts[g] = shared_counts.get(g, 0) + evidence["shared_pixels"]
                records[groups[g][0]].setdefault("depth_shared_groups", []).append(copy.deepcopy(evidence))
    return updated, records, shared_regions
