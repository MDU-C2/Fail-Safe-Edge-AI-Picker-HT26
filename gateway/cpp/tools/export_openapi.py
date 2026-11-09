"""
Export the Python gateway's OpenAPI schema to gateway/cpp/openapi/openapi.json.

The C++ gateway serves that file unchanged at /openapi.json, so the
schema stays the one FastAPI generates from the Python models. Run it
from the repository root after changing a Python route or model:

    python gateway/cpp/tools/export_openapi.py

Only the schema is built; no camera, robot or controller is contacted.
"""

import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

OUTPUT = REPOSITORY_ROOT / "gateway" / "cpp" / "openapi" / "openapi.json"


def main() -> None:
    os.chdir(REPOSITORY_ROOT)

    sys.path.insert(0, str(REPOSITORY_ROOT))

    #
    # Importing gateway.main creates the camera producer; with the
    # mock nothing is opened until the app starts, which it does not.
    #
    os.environ["GATEWAY_CAMERA_PROVIDER"] = "mock"
    os.environ["GATEWAY_ROBOT_PROVIDER"] = "mock"

    from gateway.main import app

    schema = app.openapi()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_bytes(
        (json.dumps(schema, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )

    print(f"Wrote {OUTPUT.relative_to(REPOSITORY_ROOT)} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
