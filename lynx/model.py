"""Small, explicit model contract; no exporter or ML framework in production."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


@dataclass(frozen=True)
class Model:
    directory: Path
    input_size: int
    names: tuple
    precision: str
    sha256: str

    @property
    def param(self):
        return self.directory / "model.ncnn.param"

    @property
    def weights(self):
        return self.directory / "model.ncnn.bin"

    @classmethod
    def load(cls, directory):
        directory = Path(directory).resolve()
        manifest = json.loads((directory / "manifest.json").read_text())
        size, names = manifest.get("input_size"), manifest.get("names")
        if manifest.get("schema_version") != 1 or type(size) is not int or size not in (320, 640):
            raise ValueError("Model manifest needs schema_version 1 and input_size 320 or 640.")
        if not isinstance(names, list) or not 3 <= len(names) <= 4 or any(not isinstance(n, str) or not n for n in names):
            raise ValueError("Invalid model class names.")
        if len(set(names)) != len(names) or not {"CUP", "CUP BASE", "CUP HANDLE"}.issubset(names):
            raise ValueError("Model classes must include CUP, CUP BASE and CUP HANDLE exactly once.")
        if manifest.get("precision") not in ("fp32", "int8"):
            raise ValueError("Unsupported model precision.")
        for name in ("model.ncnn.param", "model.ncnn.bin"):
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError(f"Invalid model file: {name}")
            if manifest.get("sha256", {}).get(name) != digest(path):
                raise ValueError(f"Model checksum mismatch: {name}")
        graph = (directory / "model.ncnn.param").read_text()
        anchors = sum((size // stride) ** 2 for stride in (8, 16, 32))
        if not graph.startswith("7767517\n") or not all(name in graph.split() for name in ("in0", "out0", "out1")):
            raise ValueError("Expected an NCNN segmentation graph with in0/out0/out1.")
        if not any(line.startswith("Reshape") and f"0={anchors}" in line.split() for line in graph.splitlines()):
            raise ValueError("Graph resolution disagrees with the manifest; re-export at the requested size.")
        return cls(directory, size, tuple(names), manifest["precision"], manifest["sha256"]["model.ncnn.bin"])
