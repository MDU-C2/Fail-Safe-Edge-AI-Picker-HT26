"""The shipped outside-tray grid must retain its verified calibration context."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from placement_table_reference import TableReference


class GridCalibrationProfileTests(unittest.TestCase):
    def test_shipped_grid_profile_matches_its_frozen_table_reference(self):
        app = Path(__file__).resolve().parent
        calibration_path = app.parent / "calibration" / "calibration_grid.json"
        calibration_bytes = calibration_path.read_bytes()
        raw = json.loads(calibration_bytes)
        calibration = SimpleNamespace(
            scale=raw["scale"], R=np.asarray(raw["R"]), t=np.asarray(raw["t"]),
            offset=tuple(raw["tag_offset"]),
            digest=hashlib.sha256(calibration_bytes).hexdigest())
        grid = json.loads((app / "placing_grid_outside_tray.json").read_text())
        reference = json.loads(
            (app / "placing_grid_outside_tray.table-reference.json").read_text())
        # Validate every identity field and fit bound. The grid's occupied or
        # reserved records are deliberately outside the geometric identity.
        validated = TableReference.from_dict(
            reference, calibration=calibration,
            intrinsics=raw["intrinsics"], locations=grid["locations"],
            cell_size=grid["cell_size_mm"], table_z=0.0)
        self.assertEqual(validated.metadata["source_capture"],
                         reference["source"]["capture"])


if __name__ == "__main__":
    unittest.main()
