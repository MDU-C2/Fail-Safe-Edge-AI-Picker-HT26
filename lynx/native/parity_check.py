"""Compare native detections/masks with NCNN + Ultralytics 8.4.166 on saved RGB.

Development-only dependencies: ncnn==1.0.20260526, ultralytics==8.4.166.
These packages must NOT be added to the Lynx production image.
"""

import argparse
import json
from pathlib import Path
import struct
import subprocess

import cv2
import ncnn
import numpy as np
import torch
import ultralytics
from ultralytics.data.augment import LetterBox
from ultralytics.utils import nms, ops

from smoke_check import request


def native_result(command, image):
    height, width = image.shape[:2]
    result = subprocess.run(command, input=request(1, width, height, image.tobytes()),
                            capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    offset = 0
    detections = []
    while offset < len(result.stdout):
        magic, request_id, status, json_size, mask_size = struct.unpack_from("<4sIIII", result.stdout, offset)
        offset += 20
        assert magic == b"LNXR" and status == 0
        document = json.loads(result.stdout[offset:offset + json_size])
        offset += json_size
        data = result.stdout[offset:offset + mask_size]
        offset += mask_size
        if not request_id:
            assert document["ready"]
            continue
        for item in document["detections"]:
            x, y, w, h = item["mask_bbox"]
            mask = np.zeros((height, width), dtype=bool)
            mask[y:y+h, x:x+w] = np.frombuffer(data, dtype=np.uint8, count=w*h,
                                               offset=item["mask_offset"]).reshape(h, w)
            detections.append((item["class_id"], item["score"], np.array(item["box_xyxy"]), mask))
    return detections


def reference_result(net, image, side):
    square = LetterBox((side, side), auto=False)(image=image)
    rgb = np.ascontiguousarray(square[:, :, ::-1].transpose(2, 0, 1), dtype=np.float32) / 255
    with net.create_extractor() as extractor:
        extractor.set_light_mode(True)
        extractor.input("in0", ncnn.Mat(rgb).clone())
        pred_status, prediction = extractor.extract("out0")
        proto_status, prototypes = extractor.extract("out1")
        assert pred_status == proto_status == 0
        prediction = torch.from_numpy(np.array(prediction, copy=True)).unsqueeze(0)
        prototypes = torch.from_numpy(np.array(prototypes, copy=True))
    selected = nms.non_max_suppression(prediction, conf_thres=.5, iou_thres=.7,
                                        nc=4, max_det=32, max_time_img=10)[0]
    selected[:, :4] = ops.scale_boxes((side, side), selected[:, :4], image.shape[:2])
    masks = ops.process_mask_native(prototypes, selected[:, 6:], selected[:, :4], image.shape[:2])
    return [(int(row[5]), float(row[4]), row[:4].numpy(), mask.numpy().astype(bool))
            for row, mask in zip(selected, masks) if mask.any()]


def compare(native, reference):
    assert len(native) == len(reference), f"Detection count differs: native={len(native)}, reference={len(reference)}"
    ious, centroid_errors, box_errors, score_errors = [], [], [], []
    remaining = list(reference)
    for label, score, box, mask in native:
        candidates = [(index, other) for index, other in enumerate(remaining) if other[0] == label]
        assert candidates, f"Native class {label} has no reference match"
        index, other = min(candidates, key=lambda item: np.abs(item[1][2] - box).max())
        remaining.pop(index)
        _, other_score, other_box, other_mask = other
        box_errors.append(float(np.abs(box - other_box).max()))
        score_errors.append(abs(score - other_score))
        ious.append(float(np.logical_and(mask, other_mask).sum() / np.logical_or(mask, other_mask).sum()))
        centroid = np.array(np.nonzero(mask)).mean(axis=1)
        other_centroid = np.array(np.nonzero(other_mask)).mean(axis=1)
        centroid_errors.append(float(np.linalg.norm(centroid - other_centroid)))
    result = {"detections": len(native), "minimum_mask_iou": min(ious, default=1),
              "maximum_centroid_error_px": max(centroid_errors, default=0),
              "maximum_box_error_px": max(box_errors, default=0),
              "maximum_score_error": max(score_errors, default=0)}
    assert result["minimum_mask_iou"] >= .995, result
    assert result["maximum_centroid_error_px"] <= .25, result
    assert result["maximum_box_error_px"] <= .1, result
    assert result["maximum_score_error"] <= .005, result
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--input-size", type=int, required=True)
    parser.add_argument("--images", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    assert ultralytics.__version__ == "8.4.166", "Reference version must match the export; use an isolated test environment"
    torch.set_num_threads(1)
    command = [str(args.worker), "--param", str(args.model_dir / "model.ncnn.param"),
               "--bin", str(args.model_dir / "model.ncnn.bin"), "--input-size", str(args.input_size),
               "--classes-count", "4", "--max-detections", "32"]
    with ncnn.Net() as net:
        net.opt.num_threads = 1
        net.opt.use_vulkan_compute = False
        net.opt.use_fp16_packed = False
        net.opt.use_fp16_storage = False
        net.opt.use_fp16_arithmetic = False
        net.opt.use_bf16_storage = False
        net.opt.use_winograd_convolution = False
        net.opt.use_sgemm_convolution = False
        assert net.load_param(str(args.model_dir / "model.ncnn.param")) == 0
        assert net.load_model(str(args.model_dir / "model.ncnn.bin")) == 0
        for path in args.images:
            image = cv2.imread(str(path))
            assert image is not None and image.shape[0] <= 1080 and image.shape[1] <= 1920
            native = native_result(command, image)
            reference = reference_result(net, image, args.input_size)
            print(json.dumps({"image": path.name, "model_input": args.input_size,
                              "ultralytics": ultralytics.__version__, **compare(native, reference)}), flush=True)


if __name__ == "__main__":
    main()
