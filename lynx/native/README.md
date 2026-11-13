# Native Lynx segmentation worker

`lynx-ncnn-worker` holds one NCNN network and processes one frame at a time. It
has no camera connection, web server, robot client or autonomous inference loop.
The gateway remains the single camera owner. The coordinator sends a frame only
when a fresh observation is needed.

## Build

Dependencies are C++17, NCNN with INT8 enabled and OpenCV **core + imgproc**.
No DepthAI, PyTorch, OpenCV video I/O or image codecs are linked into this worker.

```sh
cmake -S lynx/native -B build/lynx-native -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/opt/ncnn
cmake --build build/lynx-native --parallel 2
ctest --test-dir build/lynx-native --output-on-failure
cmake --install build/lynx-native --prefix /opt/lynx
```

Core checks can also run without NCNN/OpenCV:

```sh
c++ -std=c++17 -Wall -Wextra -Wpedantic lynx/native/tests.cpp -o /tmp/lynx-native-tests
/tmp/lynx-native-tests
```

`smoke_check.py` drives the real executable with blank landscape/portrait frames,
optionally repeats a recorded cup image, verifies framing/cropped masks, and
checks that a mismatched export size fails before readiness. `parity_check.py`
compares the real native output with NCNN + Ultralytics 8.4.166 on saved frames.
The latter needs development-only `ncnn==1.0.20260526` and
`ultralytics==8.4.166`; keep those packages off Lynx.

```sh
python lynx/native/smoke_check.py --worker build/lynx-native/lynx-ncnn-worker \
  --model-dir /path/to/model --input-size 320 --image /path/to/recorded-cups.jpg
python lynx/native/parity_check.py --worker build/lynx-native/lynx-ncnn-worker \
  --model-dir /path/to/model --input-size 320 --images /path/to/recorded-cups.jpg
```

Local ARM64 verification with the recovered 320 FP32 export and three saved cup
frames produced matching detection counts (11, 12, 11), minimum mask IoU 0.9999604,
and maximum centroid difference 0.00465 pixels against the exact 8.4.166 reference.
Protocol/geometry tests and the 320 worker smoke also passed address/undefined
behaviour sanitizers (the linked NCNN/OpenCV libraries were release builds).
These checks validate the port; they do not establish live Lynx memory use,
320-versus-640 cup recall, grasp accuracy or INT8 model accuracy.

A separate offline sensitivity check used both existing FP32 exports on the same
three 1920×1080 recorded frames, confidence 0.5 and IoU 0.7. Nearest distinct CUP
BASE mask centroids were paired within 100 pixels:

| Recorded frame suffix | CUP BASE count, 320 / 640 | Median centroid shift | Maximum shift |
|---|---:|---:|---:|
| `112510_674704_scan` | 7 / 8 | 7.35 px | 16.79 px |
| `112244_999960_scan` | 7 / 8 | 10.15 px | 21.26 px |
| `114849_631550_scan` | 6 / 9 | 9.68 px | 16.00 px |

CUP counts were 3/7, 4/7 and 4/8. One unmatched 640 base in each frame lies only
16–20 pixels from another 640 base, so these count differences must not be called
ground-truth missed cups. The third frame also has two separate unmatched 640
bases at confidence 0.539 and 0.550. One matched base's confidence changes from
0.908 at 640 to 0.567 at 320, with a 12.77-pixel centroid shift. These existing
exports therefore yield materially different observations; the default 320
profile still needs operator validation of cup/base association, interior depth
and robot-space grasp coordinates. This is a small model-choice sensitivity
check, not a labeled recall study or proof of physical picking accuracy.

Use the actual exported model size; changing a flag cannot resize a fixed graph:

```sh
/opt/lynx/bin/lynx-ncnn-worker \
  --param /opt/model/model.ncnn.param --bin /opt/model/model.ncnn.bin \
  --input-size 320 --classes-count 4 --threads 1 --confidence 0.5 --iou 0.7 \
  --max-detections 32
```

The model manifest is validated by the Python owner before launch. This executable
also validates the static segmentation reshapes, NCNN input/output names, actual
output dimensions and finite FP32 outputs. An initial inference must succeed
before the ready message is written. Class IDs preserve export ordering; the
owner maps them to the manifest's CUP, CUP BASE, CUP HANDLE and optional TRAY.
`--version` prints the protocol and postprocessing version without loading a model.

## Binary IPC version 1

All integer fields are **unsigned 32-bit little-endian**. stdout is reserved for
this protocol; logs and startup failures go to stderr. There is one outstanding
request, and there are no implicit retries or image queues. The owner must put
deadlines around startup, pipe writes and reads, and terminate the worker after
a timeout or broken frame. Clean stdin EOF exits. No frame data is written to disk.

Request header is 24 bytes (`struct.pack('<4sIIIII', ...)`):

| Field | Value |
|---|---|
| magic | Four ASCII bytes `LNXQ` |
| request_id | Nonzero; response echoes this ID |
| width, height | 1..1920, 1..1080 |
| payload_len | Exactly `width * height * 3` |
| reserved | Zero |

The header is immediately followed by contiguous **interleaved uint8 BGR** bytes,
row-major with no row padding. The gateway's planar BGR bytes must first be
converted to this layout by the coordinator. Invalid framing or excessive sizes
terminate before allocating the payload.

Response header is 20 bytes (`struct.unpack('<4sIIII', ...)`):

| Field | Value |
|---|---|
| magic | Four ASCII bytes `LNXR` |
| request_id | Original request ID; zero means startup ready |
| status | 0 success, 1 inference error |
| json_len | UTF-8 JSON length, maximum 65,536 bytes |
| mask_bytes | Binary payload length, maximum 64 MiB |

The header is followed by JSON and then `mask_bytes` raw bytes. Startup sends:

```json
{"ready":true,"protocol_version":1,"version":"1.0.0","postprocess":"ultralytics-8.4.166-retina","input_size":320,"classes_count":4,"max_detections":32}
```

A successful frame sends:

```json
{"width":960,"height":540,"detections":[{"class_id":1,"score":0.95,"box_xyxy":[20.2,30.1,40.7,45.4],"mask_bbox":[21,31,20,15],"mask_offset":0,"mask_length":300}],"inference_ms":1234.0}
```

Each mask is a cropped row-major uint8 array, values **0 or 1**. `mask_bbox` is
`[x, y, width, height]` in original frame/depth coordinates. `mask_length` equals
its width times height; `mask_offset` is relative to the binary payload start.
Offsets are consecutive and masks do not overlap in the payload. Pixels outside
the crop are false. Empty masks are omitted. No full-image float masks are retained.
Errors send `{"error":"description"}` with zero mask bytes. Allocation failure,
invalid framing or startup failure exits with nonzero status.

## Numeric contract and resource choices

The reference is the export's Ultralytics **8.4.166**, square static input,
`rect=False`, `retina_masks=True`, class-aware NMS and non-end-to-end segmentation.
Preprocessing uses centered letterboxing, ties-to-even resized dimensions,
114 padding, OpenCV `INTER_LINEAR`, BGR→RGB and division by 255. Box coordinates
are unpadded and divided by the letterbox gain, then clipped to the original frame.

Mask coefficients are multiplied by 32 prototype channels, once per kept detection.
Prototype padding is cropped using the reference `scale_masks` geometry. Bilinear
sampling with half-pixel coordinates and replicated edges maps logits directly
into the requested original-image box. Only then are logits thresholded at zero.
This matches `process_mask_native` geometry without allocating one full-resolution
float tensor for each object. Boundary pixels near zero can differ with floating
point accumulation; recorded-camera parity and millimetre coordinate validation
remain deployment gates, especially when quantizing or changing image size.

NCNN light mode stays enabled. Output Mats and the extractor stay alive through
postprocessing, with no cross-request tensor references or unconditional clones.
The conservative baseline disables Winograd/SGEMM weight transforms and floating
point 16 storage/arithmetic, enables supported INT8 inference, defaults to one
thread, and sets the OpenMP block time to zero. A build without OpenMP gives the
strongest single-thread bound. Use `OMP_WAIT_POLICY=PASSIVE` with libgomp builds.
All buffers are request-scoped except the reusable input byte buffer and loaded
network. Runtime allocators may retain their high-water pages; measure container
memory as well as process RSS on Lynx.

Reference implementations:

- [Ultralytics 8.4.166 mask/box operations](https://github.com/ultralytics/ultralytics/blob/v8.4.166/ultralytics/utils/ops.py)
- [NCNN extractor lifetime and default output conversion](https://github.com/Tencent/ncnn/blob/20250916/src/net.h)
- [NCNN OpenMP guidance](https://github.com/Tencent/ncnn/wiki/openmp-best-practice)
