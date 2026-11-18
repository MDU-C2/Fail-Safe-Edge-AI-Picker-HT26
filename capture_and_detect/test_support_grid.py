"""Isolated placement fixtures: tests must never modify the project grid."""

import copy
import json
from pathlib import Path
import tempfile

from placing_grid import PlacingGrid


def grid_fixture(directory, *, locations=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "test_placing_grid.json"
    if locations is None:
        locations = [{"id": 1, "x": 420, "y": -200, "z": 230, "occupied": False},
                     {"id": 2, "x": 500, "y": -200, "z": 230, "occupied": False},
                     {"id": 3, "x": 580, "y": -200, "z": 230, "occupied": False}]
    path.write_text(json.dumps({"locations": copy.deepcopy(locations)}))
    return PlacingGrid(path)


def temporary_grid(test_case, *, locations=None):
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    return grid_fixture(directory.name, locations=locations)
