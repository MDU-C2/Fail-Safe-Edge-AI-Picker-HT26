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

    drop_unused_components(schema)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_bytes(
        (json.dumps(schema, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )

    print(f"Wrote {OUTPUT.relative_to(REPOSITORY_ROOT)} ({len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
