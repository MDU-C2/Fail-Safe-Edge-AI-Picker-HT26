"""Split observed depth pixels between disjoint exact cup-mask anchors.

Nearest-mask distance is a geometry heuristic, not proof of object identity.
An unlabelled object touching a cup can still be attributed to it. The caller
must protect other detections and validate the physical cell assignment before
accepting any split. This helper never fills holes or reconstructs hidden pixels.
"""

import cv2
import numpy as np


MAX_COMPONENT_PIXELS = 131072
MIN_GROUPS = 2
MAX_GROUPS = 4
MIN_SEED_PIXELS = 32
MIN_SEED_FRACTION = 0.5
SEAM_MARGIN_PX = 1.0


def split_component(component, seeds):
    """Return ``(partitions, seam)`` in seed order, or reject with ``None``.

    Inputs must be equally shaped two-dimensional boolean arrays. The component
    crop is bounded, and two to four disjoint seed masks must lie wholly within
    it. Each seed contains at least 32 pixels. Pixels whose nearest two seeds
    differ in Euclidean image distance by at most one pixel stay unclaimed;
    exclusive exact seed pixels remain assigned even when adjacent.

    Each partition retains only eight-connected pieces touching its own seed.
    Every partition must have at least 50% exact seed coverage, otherwise the
    entire split is rejected. Unanchored pieces join the unclaimed seam. Neither
    inputs nor existing ownership are modified; the caller applies the result
    atomically after its other validation gates pass.
    """
    if (not isinstance(component, np.ndarray) or component.ndim != 2
            or component.dtype != np.bool_
            or not 0 < component.size <= MAX_COMPONENT_PIXELS
            or not isinstance(seeds, (list, tuple))
            or not MIN_GROUPS <= len(seeds) <= MAX_GROUPS):
        return None

    exact = np.zeros(component.shape, dtype=bool)
    seed_counts = []
    for seed in seeds:
        if (not isinstance(seed, np.ndarray) or seed.shape != component.shape
                or seed.dtype != np.bool_):
            return None
        count = int(np.count_nonzero(seed))
        if count < MIN_SEED_PIXELS or np.any(seed & (~component | exact)):
            return None
        exact |= seed
        seed_counts.append(count)

    distances = np.stack([
        cv2.distanceTransform((~seed).astype(np.uint8), cv2.DIST_L2,
                              cv2.DIST_MASK_PRECISE)
        for seed in seeds
    ])
    nearest = np.argmin(distances, axis=0)
    ordered = np.partition(distances, 1, axis=0)
    uncertain = (ordered[1] - ordered[0]) <= SEAM_MARGIN_PX
    nearest[~component | uncertain] = -1
    for index, seed in enumerate(seeds):
        nearest[seed] = index

    partitions = []
    claimed = np.zeros(component.shape, dtype=bool)
    for index, (seed, count) in enumerate(zip(seeds, seed_counts)):
        candidate = (nearest == index).astype(np.uint8)
        _, labels = cv2.connectedComponents(candidate, connectivity=8)
        anchor_labels = np.unique(labels[seed])
        anchor_labels = anchor_labels[anchor_labels != 0]
        partition = np.isin(labels, anchor_labels)
        pixels = int(np.count_nonzero(partition))
        if not pixels or count < pixels * MIN_SEED_FRACTION:
            return None
        partitions.append(partition)
        claimed |= partition

    return partitions, component & ~claimed & ~exact
