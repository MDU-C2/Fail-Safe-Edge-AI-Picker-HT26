# Native Lynx perception worker

`lynx-ncnn-worker` holds one NCNN network and processes one frame at a time. It
has no camera connection, web server, robot client or autonomous inference loop.
The gateway remains the single camera owner. The coordinator sends a frame only
when a fresh observation is needed. The current command-line picker uses the
`LNXP` perception extension: NCNN segmentation, object depth/coordinates and
placement assessment run in C++; the Python coordinator retains freshness,
confirmation, recovery, reservations and robot-command control. The original
`LNXQ` segmentation-only protocol remains available for compatibility.

## Build

Dependencies are C++17, NCNN with INT8 enabled and OpenCV **core + imgproc**.
No DepthAI, PyTorch, OpenCV video I/O or image codecs are linked into this worker.
JSON for Modern C++ 3.12.0 is vendored as an unmodified single header with its MIT
license; [the vendor record](vendor/nlohmann/README.md) contains source URLs and hashes.

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

The following tools exercise the **legacy v1 segmentation path**.
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

The legacy smoke script sends batched requests; a recorded-image run stalled on
the Mac development host during the 2026-11-13 checks. It is not the v2 transport
or deployment check. Use the sequential v2 `pipeline_check.py` described below
for the current command-line pipeline.

## Historical 320 FP32 evidence

The following results were recorded in commit `e3f1cfb` on **2026-11-12**, before
the 640 INT8/v2 work. They are not measurements of the current Lynx deployment.

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
exports therefore yield materially different observations; that historical 320
profile still needs operator validation of cup/base association, interior depth
and robot-space grasp coordinates. This is a small model-choice sensitivity
check, not a labeled recall study or proof of physical picking accuracy.

## Model launch

Use the actual exported model size; changing a flag cannot resize a fixed graph.
The current `quantized_2026-11-11_2308` exports use static **640×640** input:

```sh
/opt/lynx/bin/lynx-ncnn-worker \
  --param /opt/model/model.ncnn.param --bin /opt/model/model.ncnn.bin \
  --input-size 640 --classes-count 4 --threads 1 --confidence 0.5 --iou 0.7 \
  --max-detections 32
```

The model manifest is validated by the Python owner before launch. This executable
also validates the static segmentation reshapes, NCNN input/output names, actual
output dimensions and finite FP32 outputs. An initial inference must succeed
before the ready message is written. Class IDs preserve export ordering; the
owner maps them to the manifest's CUP, CUP BASE, CUP HANDLE and optional TRAY.
`--version` prints the protocol and postprocessing version without loading a model.

## Shared transport rules

All binary integer fields are **unsigned 32-bit little-endian**. stdout is reserved for
this protocol; logs and startup failures go to stderr. There is one outstanding
request, and there are no implicit retries or image queues. The owner must put
deadlines around startup, pipe writes and reads, and terminate the worker after
a timeout or broken frame. Clean stdin EOF exits. No frame data is written to disk.

After model validation and warmup, request ID zero carries a ready JSON object.
The current worker advertises `version: "2.0.0"`, `protocol_version: 1` and
`perception_version: 2`, together with `ready: true`, `ncnn_version`,
`opencv_version`, `postprocess: "ultralytics-8.4.166-retina"`, `input_size`,
`classes_count` and `max_detections`. The outer response framing remains version
1; **`perception_version: 2` is the capability required for LNXP**. A coordinator
must verify the advertised model dimensions, class count and detection limit.

## Binary IPC v2 perception extension — current picker

The request header is **32 bytes** (`struct.pack('<4sIIIIIII', ...)`):

| Field, in wire order | Value |
|---|---|
| magic | Four ASCII bytes `LNXP` |
| request_id | Nonzero; response echoes this ID |
| width, height | 1..1920, 1..1080 |
| rgb_len | Exactly `width * height * 3` |
| depth_count | 1..9 aligned depth samples |
| config_len | UTF-8 JSON length, 1..65,536 bytes |
| reserved | Zero |

The payload follows in this exact order:

1. `config_len` bytes of JSON configuration.
2. `depth_count` depth images, **oldest to newest**, each exactly
   `width * height * 2` bytes: unsigned 16-bit little-endian millimetres, row-major,
   no row padding. Zero represents unavailable depth.
3. `rgb_len` bytes of the **newest RGB frame**, in the gateway's planar BGR888p
   layout: the complete B plane, then G, then R. The worker converts this layout
   internally. Do not send v1's interleaved pixels in an LNXP request.

All depth samples must share the current aligned RGB geometry. The coordinator
collects fresh matching pairs and checks advancing sequences; sequence and
timestamp provenance remain coordinator-owned rather than part of the binary
header. It releases older depth buffers while writing them to the pipe.

Configuration used by the current picker:

| JSON field | Contract |
|---|---|
| `calibration` | Positive finite `scale`, 3×3 `R`, three-element `t`, and optional three-element `tag_offset`; coordinates are millimetres. |
| `intrinsics` | Finite `fx`, `fy`, `cx`, `cy`; focal lengths positive; integer `width` and `height` must match the request. |
| `names` | Three or four labels in export order, including CUP, CUP BASE, CUP HANDLE and optional TRAY; class count must match the loaded graph. |
| `grid_snapshot.locations` | 1..64 locations with unique positive integer `id` and finite `x`, `y`; the picker also supplies validated `z`, occupancy and reservation fields. |
| `cell_size_mm` | Finite 20..200; native default 80 when omitted. |
| `table_z_mm` | Finite table height; native default 0. |
| `table_reference` | Frozen empty-table artifact containing `plane_z_mm`; the command-line picker requires and validates the full artifact and its camera/calibration/grid identity before sending it. |

The C++ placement code checks geometry and reference-plane bounds. Full artifact
provenance, file hashes and changes during a capture are checked by the Python
coordinator; the worker does not fit a reference from an occupied scene or read
configuration files itself.

Responses use the same **20-byte LNXR header** as v1 below, with two differences:
successful perception JSON may be up to **4,194,304 bytes (4 MiB)** and
`mask_bytes` is always **zero**. The JSON contains `width`, `height`,
`perception_version: 2`, `inference_ms`, `objects` and `slots`.

- `objects` are the current detector description records, including detection
  IDs, boxes, confidence, pixel/camera/robot coordinates, depth-quality evidence,
  diameter and cup/base associations. Unavailable measurements are JSON null.
- Each object's `segmentation` retains exact holes and boundaries using
  `{"encoding":"base64-packbits-big","origin":[x,y],"size":[height,width],"data":"..."}`.
  There is no separate raw mask payload in the v2 response.
- `slots` follow the requested grid order and retain `clear`, `occupied` or
  `unknown`, reason codes, polygons, visibility/depth fractions, assigned cups
  and uncertainty diagnostics. Assessment does not change placement records.
- `inference_ms` measures segmentation preprocessing, NCNN inference and mask
  postprocessing. It **excludes** object-depth and placement work; measure the
  complete request separately when assessing latency.

V2 bounds are independent of model file size:

| Resource | Limit |
|---|---:|
| Frame | 1920×1080 |
| Depth burst | 1..9 raw aligned images |
| Kept detections | Configured 1..64, default 32 |
| Internal combined segmentation masks, including TRAY | 8,388,608 bytes |
| Retained CUP / CUP BASE / CUP HANDLE raw crop | 262,144 pixels each; 8,388,608 total |
| Placement cup-group crop / connected depth region | 262,144 / 131,072 pixels |
| Request configuration / perception response JSON | 65,536 / 4,194,304 bytes |

Oversized retained crops fail before their distance maps and coordinate vectors
are allocated, including sparse masks with large bounding rectangles. Resource
failure never authorizes a clear cell. The standalone placement test interfaces
allow larger synthetic frames (up to 8,388,608 pixels); this does not increase
the worker's 1920×1080 wire limit.

Validation or inference failures return status 1 with an error object when the
stream remains usable. Framing, startup and allocation failures terminate the
worker. The production `NativePerception` client discards its worker after any
request error, timeout or invalid response; it does not retry robot activity.

## Binary IPC v1 segmentation — compatibility

This path returns detections and cropped masks only. It remains available for
older clients and segmentation parity tools, but does not perform depth or grid
assessment.

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

The header is followed by JSON and then `mask_bytes` raw bytes. Startup readiness
is described above. A successful segmentation frame sends:

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

## C++ depth and placement ports; offline validation

`object_depth.cpp` exposes `describe_objects(detections, masks, depths, config)`.
It ports the V4-style `Detector.describe`, eroded-mask depth sampling,
cup/base association, rounding and
packed segmentation. Object robot coordinates exclude `tag_offset`, matching
the detector contract; the planner applies its configured offset. Placement
geometry applies that offset when mapping depth into the physical grid.

One depth image omits temporal fields. With multiple images, the **latest
segmentation** selects samples in every depth frame; positive mask medians are
averaged and missing samples are ignored, as in V4. There is no core-coverage
or depth-spread rejection. Per-frame Z telemetry is preserved. Older depth
images affect object coordinates only: **placement always uses the newest raw
depth**, never an average image. Five burst samples do not count as five clear
placement observations.

`placement.cpp`, `placement_common.cpp`, `placement_cups.cpp` and
`placement_depth.cpp` port the current placement rules: exact cup ownership,
connected depth regions, ambiguous splits/shared boundaries, bounded cup-edge
uncertainty, projected visibility, labelled-object protection and the frozen
table reference. Known tolerance for small unlabelled objects close to cups is
preserved; port parity is not proof that stereo artefacts and such objects can
always be distinguished. The coordinator uses one saved scan for each batch and retains placement
reservations and unfinished-movement recovery; it does not recapture or rematch
cups between transfers.

The differential runners below are development tools, not deployed services.
CMake currently builds the worker and dependency-free core tests only. Build
these additional executables with C++17 and OpenCV core/imgproc:

| Executable used below | Sources / required definition |
|---|---|
| `object-depth-harness` | `object_depth_harness.cpp`, `object_depth.cpp` |
| `placement-replay` | `placement_replay.cpp`, `placement.cpp`, `placement_common.cpp`, `placement_cups.cpp`, `placement_depth.cpp`; `-DLYNX_PLACEMENT_REPLAY_MAIN` |
| `placement-depth-parity` | `placement_depth_parity.cpp`, `placement_depth.cpp`, `placement_common.cpp`; `-DLYNX_PLACEMENT_DEPTH_PARITY_MAIN` |

Run from the repository root in a development Python environment:

```sh
python lynx/native/object_depth_parity.py \
  --harness /path/to/object-depth-harness --malformed --output-dir /tmp/object-depth-results
python lynx/native/placement_parity.py \
  --runner /path/to/placement-replay --synthetic --output /tmp/placement-results
python lynx/native/placement_parity.py \
  --runner /path/to/placement-replay --archive /path/to/Detections \
  --calibration /path/to/calibration.json --reference /path/to/grid.table-reference.json \
  --output /tmp/placement-archive-results
python lynx/native/placement_depth_parity.py --runner /path/to/placement-depth-parity
python lynx/native/pipeline_check.py --worker build/lynx-native/lynx-ncnn-worker \
  --model /path/to/manifest-model-directory --detections /path/to/detections_STAMP.json \
  --calibration /path/to/calibration.json --grid /path/to/placing-grid.json \
  --output /tmp/native-pipeline-results
```

`object_depth_parity.py` compares complete object JSON against the Python
detector, including exact segmentation and depth failure reasons. It also
accepts cached v1 responses with `--replay-config`, `--replay-response` and one or
more `--replay-depth` files. `placement_parity.py` compares full slot records;
`placement_depth_parity.py` compares owner pixels, shared regions and uncertainty
metadata, reporting Python-only cases it cannot represent rather than claiming
they ran natively.

`pipeline_check.py` exercises the real v2 executable through the lightweight
capture/store pipeline. It requires sibling `rgb_STAMP.jpg` and
`depth_STAMP.png` files plus the grid's sibling `.table-reference.json`. Its
output directory must be new. It copies grid state into scratch and verifies it
is unchanged. Repeated saved depth samples are **synthetic replay**, not fresh
camera evidence. None of these tools sends robot commands or contacts a camera.

On **2026-11-13**, object/depth validation passed 37 differential fixtures,
28 malformed/resource rejections and 14 cached FP32/INT8 saved-frame/temporal
cases. The differential comparator checks full JSON structure, text and packed
masks exactly, with a 1e-9 absolute tolerance for numeric values. The 14 cached
cases also passed direct JSON equality checks. The 37+28 cases passed ASan/UBSan with
release OpenCV libraries. This establishes conversion parity for those inputs;
it does not equate INT8 model output with FP32 output or establish live grasp
accuracy, target-device peak memory or end-to-end latency. The 640 FP32/FP16/INT8
exports have different numerical behaviour and must be assessed separately
from the historical 320 evidence above.

## Numeric contract and resource choices

The postprocessing implementation targets Ultralytics **8.4.166**, square static input,
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
Request buffers are released each iteration; the loaded network persists. In
the v2 path, RGB and segmentation payloads are released before placement, and
older depth samples are released once object coordinates have been computed.
On glibc builds, released heap pages are also trimmed between processing phases
and requests. Allocators and libraries may still retain high-water pages;
measure shared/container memory as well as process RSS on Lynx. Small INT8
weight files do not imply equally small runtime memory.

Reference implementations:

- [Ultralytics 8.4.166 mask/box operations](https://github.com/ultralytics/ultralytics/blob/v8.4.166/ultralytics/utils/ops.py)
- [NCNN extractor lifetime and default output conversion](https://github.com/Tencent/ncnn/blob/20250916/src/net.h)
- [NCNN OpenMP guidance](https://github.com/Tencent/ncnn/wiki/openmp-best-practice)
