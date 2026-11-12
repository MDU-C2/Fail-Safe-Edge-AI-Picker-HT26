#!/usr/bin/env python3
"""Stage only executable source and an explicitly selected model for Docker.

Never copy a checkout wholesale: it contains machine credentials, captures,
calibration and robot placement/recovery state. This script needs no ML packages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


MODEL_REQUIRED = ("model.ncnn.param", "model.ncnn.bin", "metadata.yaml", "manifest.json")
MODEL_OPTIONAL = ("classes.txt",)
GATEWAY_PACKAGES = (
    "calibration", "camera", "control", "events", "models", "robot", "rws", "security",
)


def copy_file(source: Path, target: Path, manifest: dict[str, str], output: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Expected a regular source file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    manifest[str(target.relative_to(output))] = hashlib.sha256(target.read_bytes()).hexdigest()


def stage(repo: Path, model: Path, output: Path) -> dict[str, str]:
    repo, model, output = repo.resolve(), model.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f"Output already exists; choose a new context directory: {output}")
    if output == repo or repo.is_relative_to(output) or model == output or model.is_relative_to(output):
        raise ValueError("Output must not contain the source repository or model")
    for name in MODEL_REQUIRED:
        if not (model / name).is_file():
            raise ValueError(f"Missing {model / name}; prepare a validated model with lynx.prepare_model first")
    if not (repo / "lynx/app.py").is_file():
        raise ValueError("Repository does not contain the Lynx launcher")
    output.mkdir(parents=True)
    manifest: dict[str, str] = {}

    def add(source: Path, relative: Path | str) -> None:
        copy_file(source, output / relative, manifest, output)

    gateway = repo / "gateway"
    for source in sorted(gateway.glob("*.py")):
        if not source.name.startswith("test_"):
            add(source, Path("app/gateway") / source.name)
    for package in GATEWAY_PACKAGES:
        for source in sorted((gateway / package).rglob("*.py")):
            if "tests" not in source.parts and not source.name.startswith("test_"):
                add(source, Path("app/gateway") / source.relative_to(gateway))

    capture = repo / "capture_and_detect"
    for source in sorted(capture.glob("*.py")):
        if not source.name.startswith(("test_", "check_")):
            add(source, Path("app/capture_and_detect") / source.name)
    for source in sorted((capture / "web_static").iterdir()):
        if source.suffix in {".html", ".css", ".js"}:
            add(source, Path("app/capture_and_detect/web_static") / source.name)
    for source in sorted((repo / "lynx").glob("*.py")):
        if not source.name.startswith("test_"):
            add(source, Path("app/lynx") / source.name)
    for source in sorted((repo / "lynx/native").rglob("*")):
        if source.is_file() and (source.suffix in {".h", ".hpp", ".c", ".cpp", ".cmake"} or source.name == "CMakeLists.txt"):
            add(source, Path("native") / source.relative_to(repo / "lynx/native"))
    for name in MODEL_REQUIRED + MODEL_OPTIONAL:
        if (model / name).exists():
            add(model / name, Path("model") / name)
    for name in ("Dockerfile", "start.sh", "requirements-arm64.lock", "measure_resources.py"):
        add(repo / "lynx/deploy" / name, name)
    (output / "source-manifest.json").write_text(json.dumps({"sha256": manifest}, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    files = stage(args.repo, args.model_dir, args.output)
    print(f"Staged {len(files)} files in {args.output.resolve()}")
    print("No credentials, calibration, captures or existing robot state were included.")


if __name__ == "__main__":
    main()
