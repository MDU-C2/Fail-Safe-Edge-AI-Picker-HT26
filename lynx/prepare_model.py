"""Add a checksum manifest to an existing fixed-size NCNN segmentation export."""
import argparse
import json
from pathlib import Path
from .model import Model, digest


def prepare(directory, input_size, names, precision):
    directory = Path(directory).resolve()
    output = directory / "manifest.json"
    document = {"schema_version": 1, "input_size": input_size, "names": names,
                "precision": precision, "sha256": {name: digest(directory / name)
                for name in ("model.ncnn.param", "model.ncnn.bin")}}
    # Never replace a manifest or silently relabel an existing deployment.
    with output.open("x") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")
    try:
        return Model.load(directory)
    except BaseException:
        output.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("--input-size", type=int, choices=(320, 640), required=True)
    parser.add_argument("--classes-file", type=Path, required=True, help="One class per line in export index order")
    parser.add_argument("--precision", choices=("fp32", "int8"), default="fp32")
    args = parser.parse_args()
    model = prepare(args.model_dir, args.input_size, args.classes_file.read_text().splitlines(), args.precision)
    print(f"Prepared {model.input_size}px {model.precision} model; checksum {model.sha256}")
    print("The manifest verifies identity and shape, not detection accuracy. Validate saved RGB/depth before live use.")


if __name__ == "__main__":
    main()
