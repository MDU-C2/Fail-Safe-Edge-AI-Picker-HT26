"""Exercise the real native executable and model without a camera or robot.

python lynx/native/smoke_check.py --worker /path/lynx-ncnn-worker \
    --model-dir /path/model --input-size 320 [--image recorded-cups.jpg]

Only --image needs NumPy/OpenCV, and those packages are not native runtime deps.
"""

import argparse
import json
import math
from pathlib import Path
import struct
import subprocess


def request(request_id, width, height, pixels):
    assert len(pixels) == width * height * 3
    return struct.pack("<4sIIIII", b"LNXQ", request_id, width, height, len(pixels), 0) + pixels


def responses(data):
    offset = 0
    messages = []
    while offset < len(data):
        assert len(data) - offset >= 20, "Truncated response header"
        magic, request_id, status, json_size, mask_size = struct.unpack_from("<4sIIII", data, offset)
        offset += 20
        assert magic == b"LNXR" and json_size <= 65536 and mask_size <= 64 * 1024 * 1024
        assert len(data) - offset >= json_size + mask_size, "Truncated response payload"
        metadata = json.loads(data[offset:offset + json_size])
        offset += json_size
        masks = data[offset:offset + mask_size]
        offset += mask_size
        assert status == 0, metadata
        if request_id:
            assert math.isfinite(metadata["inference_ms"]) and metadata["inference_ms"] >= 0
            mask_offset = 0
            assert len(metadata["detections"]) <= 32
            for detection in metadata["detections"]:
                x, y, width, height = detection["mask_bbox"]
                x1, y1, x2, y2 = detection["box_xyxy"]
                assert all(math.isfinite(value) for value in (x1, y1, x2, y2, detection["score"]))
                assert 0 <= detection["class_id"] <= 3 and 0 <= detection["score"] <= 1
                assert 0 <= x < x + width <= metadata["width"]
                assert 0 <= y < y + height <= metadata["height"]
                assert x >= x1 and y >= y1 and x + width - 1 < x2 and y + height - 1 < y2
                assert detection["mask_offset"] == mask_offset
                assert detection["mask_length"] == width * height
                content = masks[mask_offset:mask_offset + width * height]
                assert len(content) == width * height and set(content) <= {0, 1} and 1 in content
                mask_offset += width * height
            assert mask_offset == mask_size, "Unclaimed mask bytes"
        else:
            assert metadata["ready"] is True and metadata["protocol_version"] == 1 and not masks
        messages.append((request_id, metadata))
    return messages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--input-size", type=int, required=True)
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    command = [str(args.worker), "--param", str(args.model_dir / "model.ncnn.param"),
               "--bin", str(args.model_dir / "model.ncnn.bin"), "--input-size", str(args.input_size),
               "--classes-count", "4"]
    frames = [(960, 540, bytes([114]) * (960 * 540 * 3)),
              (481, 640, bytes([0]) * (481 * 640 * 3))]
    if args.image:
        import cv2
        image = cv2.imread(str(args.image))
        assert image is not None, f"Cannot open {args.image}"
        scale = min(1, 1920 / image.shape[1], 1080 / image.shape[0])
        if scale < 1:
            image = cv2.resize(image, (round(image.shape[1] * scale), round(image.shape[0] * scale)))
        frames.extend([(image.shape[1], image.shape[0], image.tobytes())] * 3)
    data = b"".join(request(index, *frame) for index, frame in enumerate(frames, 1))
    run = subprocess.run(command, input=data, capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    messages = responses(run.stdout)
    assert [entry[0] for entry in messages] == list(range(len(frames) + 1))
    assert messages[0][1]["input_size"] == args.input_size and messages[0][1]["classes_count"] == 4
    if args.image:
        assert messages[-1][1]["detections"], "Recorded frame unexpectedly produced no detections"
        assert messages[-1][1]["detections"] == messages[-2][1]["detections"] == messages[-3][1]["detections"], \
            "Repeated inference must not retain/overwrite another request's tensors"
    wrong_size = 640 if args.input_size == 320 else 320
    mismatch = subprocess.run(command[:-4] + ["--input-size", str(wrong_size), "--classes-count", "4"],
                              input=b"", capture_output=True, timeout=30)
    assert mismatch.returncode != 0 and not mismatch.stdout, "Mismatched model graph must fail before ready"
    print(json.dumps({"model_input": args.input_size, "requests": len(frames),
                      "detections": [len(value["detections"]) for _, value in messages[1:]],
                      "inference_ms": [round(value["inference_ms"], 1) for _, value in messages[1:]],
                      "mismatched_graph_rejected": True}))


if __name__ == "__main__":
    main()
