"""Launch the independent browser observer from the repository root."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "capture_and_detect"))

from observer_app import main

if __name__ == "__main__":
    main()
