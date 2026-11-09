#!/usr/bin/env python3
r"""Benchmark the cup segmentation model on saved RGB images; no gateway needed.

Example (from the project repository root, in its existing Python environment):
  python benchmark_yolo.py --model capture_and_detect/best.pt \
      --images data/images/rgb --device cpu --label mac_cpu

Requires the working deployment's ultralytics, torch and opencv packages.
NCNN model directories additionally require ncnn and their metadata.yaml.
"""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(values):
    """Sample SD and linearly interpolated 95th percentile, in milliseconds."""
    ordered = sorted(values)
    position = 0.95 * (len(ordered) - 1)
    lower, upper = math.floor(position), math.ceil(position)
    return {
        "count": len(ordered),
        "mean_ms": statistics.mean(ordered),
        "median_ms": statistics.median(ordered),
        "sample_sd_ms": statistics.stdev(ordered) if len(ordered) > 1 else None,
        "p95_ms": ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def cpu_temperature():
    """Linux thermal-zone readings; unavailable on macOS without extra software."""
    readings = {}
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            name = (zone / "type").read_text().strip()
            readings[f"{zone.name}:{name}"] = float((zone / "temp").read_text()) / 1000
        except (OSError, ValueError):
            continue
    return readings or None


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def write_json(path, content):
    path.write_text(json.dumps(content, indent=2, allow_nan=False) + "\n")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, type=Path,
                        help="Existing fine-tuned .pt file or *_ncnn_model directory")
    parser.add_argument("--images", required=True, type=Path,
                        help="One RGB image or a folder containing only RGB test images")
    parser.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    parser.add_argument("--label", required=True, help="Example: mac_cpu or pi_ncnn")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=5,
                        help="Complete measured passes over every image (default: 5)")
    parser.add_argument("--classes", nargs="+", type=int, default=[1],
                        help="Class IDs to retain; default: 1 (CUP BASE)")
    parser.add_argument("--output", type=Path, default=Path("benchmark_results"))
    args = parser.parse_args()
    if args.imgsz <= 0 or args.imgsz % 32:
        parser.error("--imgsz must be a positive multiple of 32.")
    if args.warmup < 1 or args.repeats < 1:
        parser.error("--warmup and --repeats must be at least 1.")
    args.model = args.model.expanduser().resolve()
    args.images = args.images.expanduser().resolve()
    if not args.model.exists() or not args.images.exists():
        parser.error("Model or images path does not exist. Check the paths.")
    if not (args.model.is_dir() or args.model.suffix.lower() == ".pt"):
        parser.error("Use your .pt checkpoint or an NCNN export directory.")
    if args.model.is_dir() and not args.model.name.endswith("_ncnn_model"):
        parser.error("The NCNN directory name must end with _ncnn_model.")
    if args.model.is_dir() and args.device != "cpu":
        parser.error("Use --device cpu for this NCNN benchmark.")
    return args


def main():
    args = arguments()
    # Preserve the installed deployment environment: do not auto-install packages.
    os.environ["YOLO_AUTOINSTALL"] = "false"
    try:
        import cv2
        import torch
        from ultralytics import YOLO
        from ultralytics.models.yolo.segment.predict import SegmentationPredictor
    except ImportError as exc:
        raise SystemExit(
            "Activate the Python environment where your model already runs.\n"
            f"Missing dependency: {exc}"
        ) from exc

    if args.device == "mps" and not torch.backends.mps.is_available():
        raise SystemExit("MPS is unavailable in this Python environment. Use --device cpu.")

    export_metadata = None
    if args.model.is_dir():
        import yaml
        try:
            import ncnn  # Also accepts manually installed modules without pip metadata.
        except ImportError as exc:
            raise SystemExit("NCNN is missing. Activate your working NCNN environment.") from exc
        metadata_file = args.model / "metadata.yaml"
        if not metadata_file.is_file():
            raise SystemExit("NCNN metadata.yaml is required to verify the export size.")
        export_metadata = yaml.safe_load(metadata_file.read_text())
        if export_metadata.get("task") != "segment":
            raise SystemExit("This NCNN export is not marked as a segmentation model.")
        if export_metadata.get("imgsz") != [args.imgsz, args.imgsz]:
            raise SystemExit(
                f"Export size is {export_metadata.get('imgsz')}, requested "
                f"{args.imgsz}x{args.imgsz}. Use a matching export; do not relabel it."
            )
        model_files = sorted(args.model.glob("*.param")) + sorted(args.model.glob("*.bin"))
        if not any(p.suffix == ".param" for p in model_files) or not any(
            p.suffix == ".bin" for p in model_files
        ):
            raise SystemExit("The NCNN directory must contain .param and .bin files.")
        model_files.append(metadata_file)
    else:
        model_files = [args.model]

    extensions = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    paths = ([args.images] if args.images.is_file() else
             sorted(p for p in args.images.iterdir()
                    if p.is_file() and p.suffix.lower() in extensions))
    if not paths:
        raise SystemExit("No RGB image files found.")
    images, manifest = [], []
    for path in paths:
        frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if frame is None:
            raise SystemExit(f"Cannot decode image: {path}")
        images.append(frame)
        manifest.append({"file": path.name, "sha256": sha256(path),
                         "width": frame.shape[1], "height": frame.shape[0]})
    print(f"Loaded {len(images)} images ({sum(im.nbytes for im in images) / 2**20:.1f} MiB).",
          flush=True)

    def synchronize():
        if args.device == "mps":
            torch.mps.synchronize()

    class TimedPredictor(SegmentationPredictor):
        """Time the backend call, including completion of asynchronous MPS work."""

        def inference(self, im, *call_args, **call_kwargs):
            self.benchmark_shape = [int(dimension) for dimension in im.shape]
            self.benchmark_dtype = str(im.dtype)
            expected = [1, 3, args.imgsz, args.imgsz]
            if self.benchmark_shape != expected:
                raise RuntimeError(f"Actual input {self.benchmark_shape}, expected {expected}.")
            if im.device.type != args.device:
                raise RuntimeError(f"Actual device {im.device}, requested {args.device}.")
            synchronize()
            started = time.perf_counter_ns()
            predictions = super().inference(im, *call_args, **call_kwargs)
            synchronize()
            self.benchmark_inference_ms = (time.perf_counter_ns() - started) / 1e6
            return predictions

    model = YOLO(str(args.model), task="segment")
    settings = dict(
        imgsz=args.imgsz, device=args.device, batch=1, rect=False,
        conf=0.50, iou=0.70, max_det=300, classes=args.classes,
        retina_masks=True, augment=False,
        save=False, show=False, verbose=False,
    )

    def predict(frame):
        synchronize()
        started = time.perf_counter_ns()
        result = model.predict(source=frame, predictor=TimedPredictor, **settings)[0]
        # Includes transfer to host memory, so masks are ready for later coordinates.
        result = result.cpu()
        synchronize()
        prediction_ms = (time.perf_counter_ns() - started) / 1e6
        detections = len(result.boxes) if result.boxes is not None else 0
        masks = len(result.masks) if result.masks is not None else 0
        if detections != masks:
            raise RuntimeError(f"Expected segmentation: {detections} boxes but {masks} masks.")
        return model.predictor.benchmark_inference_ms, prediction_ms, detections, masks

    temperature_before = cpu_temperature()
    print(f"Warm-up: {args.warmup} calls (excluded from results)...", flush=True)
    for index in range(args.warmup):
        predict(images[index % len(images)])
        if index == 0:
            names = model.predictor.model.names
            if not isinstance(names, dict):
                names = dict(enumerate(names))
            if any(class_id not in names for class_id in args.classes):
                raise SystemExit(f"Invalid class IDs. Model classes: {names}")
            if args.classes == [1] and str(names[1]).upper().strip() != "CUP BASE":
                raise SystemExit(f"Class 1 is {names[1]!r}, not CUP BASE. Check your checkpoint.")
        print(f"  warm-up {index + 1}/{args.warmup}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    label = re.sub(r"[^A-Za-z0-9_-]", "_", args.label)
    output = args.output.expanduser().resolve() / f"{label}_{args.imgsz}_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    backend = model.predictor.model
    net = getattr(backend, "net", None)
    ncnn_options = {}
    if net is not None:
        for key in ("num_threads", "use_vulkan_compute", "use_fp16_packed",
                    "use_fp16_storage", "use_fp16_arithmetic"):
            value = getattr(net.opt, key, None)
            ncnn_options[key] = value
    metadata = {
        "label": args.label, "timestamp_utc": stamp,
        "model_format": "ncnn" if args.model.is_dir() else "pytorch",
        "model_files": {p.name: sha256(p) for p in model_files},
        "export_metadata": export_metadata,
        "images": manifest, "settings": settings, "class_names": names,
        "input_tensor_shape": model.predictor.benchmark_shape,
        "input_tensor_dtype": model.predictor.benchmark_dtype,
        "warmup_calls_excluded": args.warmup, "repeats": args.repeats,
        "planned_measured_calls": args.repeats * len(images),
        "system": {"platform": platform.platform(), "machine": platform.machine(),
                   "python": platform.python_version(), "logical_cpus": os.cpu_count(),
                   "torch_threads": torch.get_num_threads(),
                   "opencv_threads": cv2.getNumThreads(),
                   "mps_cpu_fallback_env": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK"),
                   "ncnn_options": ncnn_options},
        "versions": {p: package_version(p) for p in ("ultralytics", "torch", "numpy", "ncnn")},
        "opencv_version": cv2.__version__,
        "temperature_before_warmup_c": temperature_before,
        "temperature_after_warmup_c": cpu_temperature(),
        "timing_definitions": {
            "backend_inference_ms": "Predictor inference method, with MPS completion "
                                    "synchronization; includes backend adapter overhead.",
            "prediction_ms": "model.predict plus result.cpu: preprocessing, backend "
                             "inference, mask postprocessing, host transfer and API overhead.",
            "excluded": "Model initialization, warm-up, image file reading/decoding, "
                        "camera/HTTP acquisition, plotting, coordinates and robot operations.",
            "schedule": "Consecutive single-image calls, sorted images repeated in full passes; "
                        "console/CSV/temperature recording occurs outside each timed call.",
        },
    }
    write_json(output / "run_info.json", metadata)
    fields = ("repeat", "image", "backend_inference_ms", "prediction_ms",
              "detections", "masks")
    rows, temperature_by_repeat = [], []
    print(f"Measuring {args.repeats * len(images)} calls. Output: {output}", flush=True)
    with (output / "measurements.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        stream.flush()
        for repeat in range(1, args.repeats + 1):
            for index, frame in enumerate(images):
                inner, total, detections, masks = predict(frame)
                row = dict(zip(fields, (repeat, paths[index].name, inner, total,
                                        detections, masks)))
                rows.append(row)
                writer.writerow(row)
                stream.flush()
                print(f"[{len(rows)}/{args.repeats * len(images)}] {paths[index].name}: "
                      f"backend {inner:.1f} ms | prediction {total:.1f} ms | "
                      f"{masks} masks", flush=True)
            temperature_by_repeat.append({"repeat": repeat, "celsius": cpu_temperature()})

    def metrics(subset):
        return {key: describe([row[key] for row in subset])
                for key in ("backend_inference_ms", "prediction_ms")}

    summary = {
        **metadata, "status": "complete", "results": metrics(rows),
        "per_repeat": {str(r): metrics([row for row in rows if row["repeat"] == r])
                       for r in range(1, args.repeats + 1)},
        "calls_without_detections": sum(row["detections"] == 0 for row in rows),
        "temperature_by_repeat_c": temperature_by_repeat,
        "temperature_after_c": cpu_temperature(),
    }
    write_json(output / "summary.json", summary)
    for key, numbers in summary["results"].items():
        print(f"{key}: mean {numbers['mean_ms']:.2f} ms | "
              f"median {numbers['median_ms']:.2f} ms | p95 {numbers['p95_ms']:.2f} ms")
    if summary["calls_without_detections"] == len(rows):
        print("NOTE: No cups were detected. Use representative cup images before "
              "reporting full segmentation prediction latency.")
    print(f"\nSend back these two files:\n{output / 'summary.json'}\n"
          f"{output / 'measurements.csv'}")


if __name__ == "__main__":
    main()
