"""Single-device cup picker: shared Python gateway and native NCNN inference."""

from pathlib import Path
import sys

# The existing picking modules are executable scripts with sibling imports.
_picker = str(Path(__file__).resolve().parent.parent / "capture_and_detect")
if _picker not in sys.path:
    sys.path.insert(0, _picker)
