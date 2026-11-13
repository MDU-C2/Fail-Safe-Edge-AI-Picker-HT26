#!/usr/bin/env python3
"""Package a locally built ARM64 image without starting its application.

Use Docker's merged root filesystem export, avoiding the vendor OCI unpacker's
unhandled nested manifests, layer whiteouts and metadata differences.
Run on Linux/WSL with docker, squashfs-tools, u-boot-tools, device-tree-compiler
and file installed,
or use --container-tools with Docker Desktop. No hardware is contacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally built image, preferably an immutable image ID")
    parser.add_argument("--output", type=Path, required=True, help="New .wmopkg output filename")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--container-tools", action="store_true", help="Run Linux packaging tools in an isolated Docker container")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.version):
        parser.error("Version must contain only letters, digits, dots, underscores or hyphens")
    if args.signing_key and not args.signing_key.is_file():
        parser.error("Signing key does not exist")
    output = args.output.resolve()
    if output.exists():
        parser.error(f"Refusing to overwrite {output}")
    required = ("docker",) if args.container_tools else ("docker", "mksquashfs", "mkimage", "dtc", "file")
    for command in required:
        if shutil.which(command) is None:
            parser.error(f"Missing {command}; use the documented Linux/WSL packaging environment")
    generator = Path(__file__).resolve().parents[2] / "gateway/docker/generate-container.py"
    if not generator.is_file():
        parser.error(f"Missing vendor package generator: {generator}")
    inspection = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image], text=True))[0]
    if inspection.get("Architecture") != "arm64" or inspection.get("Os") != "linux":
        parser.error("Lynx requires a linux/arm64 image")
    if args.container_tools:
        deploy = Path(__file__).resolve().parent
        subprocess.run(["docker", "build", "--file", str(deploy / "Dockerfile.package"),
                        "--tag", "lynx-picker-packager:local", str(deploy)], check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    container = None
    try:
        with tempfile.TemporaryDirectory(prefix="lynx-package-") as work:
            workdir = Path(work)
            container = subprocess.check_output(
                ["docker", "create", inspection["Id"]], text=True,
            ).strip()
            rootfs = workdir / "rootfs.tar"
            subprocess.run(["docker", "export", "--output", str(rootfs), container], check=True)
            if args.container_tools:
                command = ["docker", "run", "--rm", "--network", "none",
                    "--mount", f"type=bind,src={workdir},dst=/work",
                    "--mount", f"type=bind,src={generator},dst=/tools/generate-container.py,readonly"]
                if args.signing_key:
                    command.extend(["--mount", f"type=bind,src={args.signing_key.resolve()},dst=/keys/signing.key,readonly"])
                command.extend(["lynx-picker-packager:local", "python3", "/tools/generate-container.py", "-i", "/work/rootfs.tar"])
            else:
                command = [sys.executable, str(generator), "-i", str(rootfs)]
            command.extend(["-t", "rootfs",
                "-n", "lynx-picker", "-v", args.version,
                "-d", "Lynx cup picker: gateway, coordinator and NCNN worker",
            ])
            if args.signing_key:
                command.extend(["-k", "/keys/signing.key" if args.container_tools else str(args.signing_key.resolve())])
            subprocess.run(command, cwd=workdir, check=True)
            package = workdir / f"lynx-picker-{args.version}.wmopkg"
            if not package.is_file() or package.stat().st_size == 0:
                raise RuntimeError("Vendor generator did not create a nonempty package; inspect its output")
            shutil.copyfile(package, output)
            digest = hashlib.sha256()
            with output.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            output.with_suffix(output.suffix + ".json").write_text(json.dumps({
                "image_id": inspection["Id"], "architecture": "linux/arm64",
                "version": args.version, "command": "/start.sh", "working_directory": "/app",
                "persistent_mount": "/state", "default_mode": "dry-run",
                "package_sha256": digest.hexdigest(), "signed": bool(args.signing_key),
            }, indent=2) + "\n")
    finally:
        if container:
            subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)
    print(f"Created {output}; the container was never started")


if __name__ == "__main__":
    main()
