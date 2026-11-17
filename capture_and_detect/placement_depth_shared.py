"""Find thin observed boundaries shared by two accepted cup-depth partitions.

This is a bounded geometry heuristic, not proof of cup identity. An unlabelled
object touching cups may still resemble their shared boundary. The caller must
validate object protection, seed coverage and adverse physical-area votes for
every affected cup before using any returned mask. This module never changes
ownership, fills missing depth, or chooses one cup as the boundary's owner.
"""

import cv2
import numpy as np

from placement_depth_split import SEAM_MARGIN_PX


MAX_COMPONENT_PIXELS = 131072
MAX_PARTITIONS = 4
NEIGHBOR_RADIUS_PX = 2
MAX_NEIGHBOR_DISTANCE_MM = 12.0
MAX_SHARED_FRACTION = 0.05


def _surface_neighbors(points, finite, partition, candidate):
    """Find candidate pixels with an observed nearby sample of this partition.

    The fixed 5x5 neighbourhood is scanned one offset at a time. Only selected
    point pairs are materialized, rather than an HxWx25x3 distance array.
    """
    height, width = candidate.shape
    supported = np.zeros(candidate.shape, dtype=bool)
    radius = NEIGHBOR_RADIUS_PX
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            target = (slice(max(0, -dy), min(height, height - dy)),
                      slice(max(0, -dx), min(width, width - dx)))
            source = (slice(max(0, dy), min(height, height + dy)),
                      slice(max(0, dx), min(width, width + dx)))
            selected = (candidate[target] & ~supported[target] & partition[source]
                        & finite[target] & finite[source])
            if not selected.any():
                continue
            with np.errstate(over="ignore", invalid="ignore"):
                delta = points[target][selected] - points[source][selected]
                close = np.sum(delta * delta, axis=1) <= MAX_NEIGHBOR_DISTANCE_MM ** 2
            target_support = supported[target]
            target_support[selected] |= close
    return supported


def shared_boundaries(component, seeds, partitions, seam, original_owners, points):
    """Return ``[(first_index, second_index, mask), ...]`` or an empty list.

    Masks are crop-shaped booleans; ``points`` is HxWx3 physical millimetres.
    The two to four accepted partitions must be disjoint observed masks, each
    containing its own nonempty exact seed. Only unowned, non-exact seam pixels
    within two Chebyshev pixels of exactly two partitions are considered. The
    nearest two exact-seed distances must differ by at most the original split
    margin; discarded nearest-mask islands are not shared boundaries.

    Each connected candidate piece must touch both partitions directly through
    observed eight-neighbour pixels. Every pixel must also have a sample within
    12 mm in EACH partition inside that 5x5 neighbourhood. One unsupported
    pixel rejects its entire candidate piece. Conflicted partition samples
    cannot provide contact or physical support. The caller must normalize any
    foreign positive partition ownership to a negative conflict before calling.
    Holes, prior ownership/conflicts,
    three-way junctions and detached pieces never receive shared ownership.

    All returned masks together must occupy at most 5% of the component. Input
    shape/type/resource failures return no masks. Inputs remain unchanged;
    seed-coverage and physical-area acceptance belong to the caller.
    """
    if (not isinstance(component, np.ndarray) or component.ndim != 2
            or component.dtype != np.bool_
            or not 0 < component.size <= MAX_COMPONENT_PIXELS
            or not isinstance(seeds, (list, tuple))
            or not isinstance(partitions, (list, tuple))
            or not 2 <= len(partitions) <= MAX_PARTITIONS
            or len(seeds) != len(partitions)
            or not isinstance(seam, np.ndarray) or seam.shape != component.shape
            or seam.dtype != np.bool_
            or not isinstance(original_owners, np.ndarray)
            or original_owners.shape != component.shape
            or not np.issubdtype(original_owners.dtype, np.signedinteger)
            or not isinstance(points, np.ndarray) or points.shape != component.shape + (3,)
            or not np.issubdtype(points.dtype, np.number) or np.iscomplexobj(points)):
        return []
    component_pixels = int(np.count_nonzero(component))
    if not component_pixels:
        return []
    claimed = np.zeros(component.shape, dtype=bool)
    exact = np.zeros_like(claimed)
    for seed, partition in zip(seeds, partitions):
        if (not isinstance(seed, np.ndarray) or seed.shape != component.shape
                or seed.dtype != np.bool_
                or not isinstance(partition, np.ndarray) or partition.shape != component.shape
                or partition.dtype != np.bool_
                or not seed.any() or np.any(seed & ~partition)
                or np.any(partition & (~component | claimed))):
            return []
        exact |= seed
        claimed |= partition

    candidate = seam & component & ~exact & ~claimed & (original_owners == 0)
    if not candidate.any():
        return []
    # The split helper also puts unanchored nearest-mask islands into ``seam``.
    # Reconstruct only its distance-uncertain subset, with bounded arrays, so
    # those discarded islands cannot become shared by proximity alone.
    nearest = np.full(component.shape, np.inf, dtype=np.float32)
    second_nearest = np.full_like(nearest, np.inf)
    for seed in seeds:
        distance = cv2.distanceTransform((~seed).astype(np.uint8), cv2.DIST_L2,
                                         cv2.DIST_MASK_PRECISE)
        second_nearest = np.minimum(second_nearest, np.maximum(nearest, distance))
        nearest = np.minimum(nearest, distance)
    candidate &= (second_nearest - nearest) <= SEAM_MARGIN_PX
    if not candidate.any():
        return []
    neighborhood = np.ones((2 * NEIGHBOR_RADIUS_PX + 1,) * 2, dtype=np.uint8)
    near = [cv2.dilate(partition.astype(np.uint8), neighborhood).astype(bool)
            for partition in partitions]
    count = np.zeros(component.shape, dtype=np.uint8)
    for nearby in near:
        count += nearby
    candidate &= count == 2
    if not candidate.any():
        return []
    # Float conversion is bounded by the crop limit and avoids unsigned integer
    # subtraction wrapping when callers provide integer physical samples.
    coordinates = points.astype(np.float64, copy=False)
    finite = np.isfinite(coordinates).all(axis=2)
    valid_support = [partition & (original_owners >= 0) for partition in partitions]
    support = [_surface_neighbors(coordinates, finite, partition, candidate & nearby)
               for partition, nearby in zip(valid_support, near)]
    adjacent = [cv2.dilate(partition.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
                for partition in valid_support]
    results = []
    total = 0
    for first in range(len(partitions)):
        for second in range(first + 1, len(partitions)):
            pair = candidate & near[first] & near[second]
            if not pair.any():
                continue
            _, labels, stats, _ = cv2.connectedComponentsWithStats(
                pair.astype(np.uint8), connectivity=8)
            accepted = np.zeros(component.shape, dtype=bool)
            for label in range(1, len(stats)):
                x, y, width, height, pixels = stats[label]
                crop = (slice(y, y + height), slice(x, x + width))
                piece = labels[crop] == label
                if (not np.any(piece & adjacent[first][crop])
                        or not np.any(piece & adjacent[second][crop])
                        or not np.all((support[first][crop] & support[second][crop])[piece])):
                    continue
                accepted[crop] |= piece
                total += int(pixels)
            if accepted.any():
                results.append((first, second, accepted))
    return results if total <= component_pixels * MAX_SHARED_FRACTION else []
