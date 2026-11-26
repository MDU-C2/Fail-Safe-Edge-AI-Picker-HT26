"""
Export the Python gateway's OpenAPI schema to gateway/cpp/openapi/openapi.json.

The C++ gateway serves that file unchanged at /openapi.json, so the
schema stays the one FastAPI generates from the Python models. Run it
from the repository root after changing a Python route or model:

    python gateway/cpp/tools/export_openapi.py

Only the schema is built; no camera, robot or controller is contacted.
"""

import copy
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

OUTPUT = REPOSITORY_ROOT / "gateway" / "cpp" / "openapi" / "openapi.json"

#
# Python routes the C++ gateway leaves out by design.
#
LEFT_OUT_PATHS = (
    "/api/v1/robot/command",
)


def drop_unused_components(
    schema: dict,
) -> None:
    """Remove component schemas no remaining path refers to, even
    through other components."""

    components = schema.get("components", {}).get("schemas", {})

    def references(value: object) -> set[str]:
        text = json.dumps(value)

        return {
            name
            for name in components
            if f'"#/components/schemas/{name}"' in text
        }

    used = references(schema["paths"])
    pending = list(used)

    while pending:
        for name in references(components[pending.pop()]):
            if name not in used:
                used.add(name)
                pending.append(name)

    for name in list(components):
        if name not in used:
            del components[name]


def add_cpp_only_paths(
    schema: dict,
) -> None:
    """Add the routes only the C++ gateway has.

    GET /camera/distortion is built from GET /camera/intrinsics
    (same authentication and parameters) and placed right after it.
    """

    intrinsics_path = "/api/v1/camera/intrinsics"
    distortion_path = "/api/v1/camera/distortion"

    operation = copy.deepcopy(schema["paths"][intrinsics_path]["get"])

    operation["summary"] = "Get the RGB camera lens distortion"
    operation["description"] = (
        "CAM_A's distortion model and coefficients from the device calibration "
        "(C++ gateway only). The RGB and depth frames are not undistorted; the "
        "coefficients apply to normalised coordinates, so they go with "
        "GET /camera/intrinsics at any frame size."
    )
    operation["operationId"] = "get_camera_distortion_api_v1_camera_distortion_get"
    operation["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/CameraDistortion",
    }

    schema["components"]["schemas"]["CameraDistortion"] = {
        "title": "CameraDistortion",
        "type": "object",
        "required": [
            "model",
            "coefficients",
        ],
        "properties": {
            "model": {
                "title": "Model",
                "type": "string",
                "enum": [
                    "perspective",
                    "fisheye",
                    "equirectangular",
                    "radial_division",
                ],
                "description": "DepthAI's camera model; perspective is OpenCV's pinhole model (cv2.undistortPoints), fisheye cv2.fisheye.",
            },
            "coefficients": {
                "title": "Coefficients",
                "type": "array",
                "items": {
                    "type": "number",
                },
                "description": "OpenCV order: k1, k2, p1, p2, k3, k4, k5, k6, s1, s2, s3, s4, tx, ty.",
            },
        },
    }

    paths = {}

    for path, item in schema["paths"].items():
        paths[path] = item

        if path == intrinsics_path:
            paths[distortion_path] = {"get": operation}

    schema["paths"] = paths


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

    #
    # The C++ gateway has no legacy POST /robot/command.
    #
    for path in LEFT_OUT_PATHS:
        schema["paths"].pop(path, None)

    add_cpp_only_paths(schema)

    drop_unused_components(schema)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_bytes(
        (json.dumps(schema, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )

    print(f"Wrote {OUTPUT.relative_to(REPOSITORY_ROOT)} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
