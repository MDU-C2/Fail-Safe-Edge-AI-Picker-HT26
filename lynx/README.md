# Run the cup picker and gateway on Lynx

This deployment runs the existing robot gateway, picking coordinator and dashboard
in **one Python process**, plus **one persistent native NCNN worker**. The gateway
owns the OAK camera. All inference and picking decisions stay on Lynx. There is no
remote model, failover service, Ollama, PyTorch or Ultralytics at runtime.

The initial profile uses the recovered **320×320 FP32 segmentation model**,
**960×540 RGB and aligned depth at 2 FPS**, and one inference thread. It is a
candidate configuration for hardware validation, not a claim that the full stack
already fits the Lynx memory allowance.

Version 0.1.0 has now been deployed on the 485 MiB Lynx and completed three
synthetic-camera scans with the actual native model. Real OAK startup is blocked
by camera availability; no physical picking was tested. See the
[2026-11-06 deployment check](deploy/LIVE_CHECK_2026-11-06.md) for measured memory,
timings and remaining checks.

## What reduces resource use

| Change | Effect |
| --- | --- |
| Gateway and coordinator share one server on port 8080 | Removes a second Python/server process |
| Native NCNN child, loaded once | Removes the PyTorch/Ultralytics runtime and repeated model loading |
| 320 export instead of 640 | Reduces model activation and prototype sizes; weights remain FP32 |
| 960×540 instead of 1920×1080 camera frames | RGB/depth payload per pair falls from 9.89 MiB to 2.47 MiB |
| Direct access to the gateway's latest immutable pair | No internal camera HTTP polling, download buffers or extra depth copy |
| Sequential inference and cropped binary masks | Bounded requests and mask storage; no Python full-frame float masks |
| One Uvicorn worker, bounded numerical threads, no access log | Avoids duplicate services and unnecessary continuous work |
| Captures on persistent storage, with a storage budget | Avoids filling WeOS's RAM-backed writable layer |

The frame-payload figures are exact byte counts, **not total process memory**.
DepthAI, NCNN activations, native libraries, camera reconnects and allocator caches
still need measurement. A profiler is included for actual process/cgroup usage.
The camera watchdog, robot lease renewal, control-session fencing, freshness,
calibration, depth-quality, placement and recovery checks remain active.

The 320 and 640 exports are not interchangeable for picking accuracy. In three
saved full-resolution frames, 320 produced fewer CUP BASE detections and matched
base centres shifted by up to 21.26 pixels compared with 640. Some extra 640
detections overlap, so this is not a labeled recall result. Start with the 640
export when validating continuity with the existing picker if memory permits;
accept 320 only after checking actual cup associations, depth and robot XYZ.

The server provides the gateway at `/api/v1` and the existing dashboard at `/`.
The coordinator uses local HTTP for authenticated robot commands; camera data stays
in process. WebSocket event streaming is disabled in this lean launcher; the
dashboard uses its normal HTTP state polling. Language and named visual matching
are disabled; cup selection and the existing pick/handover/place workflow remain.

## Prepare deployment inputs

Run these commands from the repository root with Python 3.11 or later. Build and
package details, including ARM64 dependencies, are in [deploy/README.md](deploy/README.md).

1. Choose the actual 320 NCNN export. It must contain `model.ncnn.param`,
   `model.ncnn.bin`, `metadata.yaml` and `classes.txt` in export index order.
   Generate its manifest once:

   ```sh
   python -m lynx.prepare_model "$MODEL_DIR" \
     --input-size 320 --classes-file "$MODEL_DIR/classes.txt" --precision fp32
   ```

   The recovered local export already has this manifest. Startup checks file
   hashes, graph resolution and native output shapes. Changing a 640 model's
   input-size argument does not create a 320 export.

2. Create a **new** state directory using the installed camera's real calibration
   and the current placement grid. Preparation copies the files without resetting
   occupied/reserved locations:

   ```sh
   python -m lynx.prepare_state --state-dir "$NEW_STATE_DIR" \
     --calibration calibration/calibration.json \
     --placing-grid capture_and_detect/placing_grid.json
   ```

   This creates distinct private gateway and dashboard credentials. A new
   deployment begins with recovery required; inspect the robot and acknowledge
   recovery through the existing dashboard before use. Reuse the same state
   directory on every restart/update. Do not run the old coordinator concurrently.
   Preparation refuses to overwrite an existing directory.

3. Build the ARM64 image from an isolated context, then mount the prepared state
   directory from persistent SD storage at `/state`. Real-camera startup on Linux
   refuses a state path that is not a mount point. A `.wmopkg` does not configure
   that mount or the application's network access automatically.

## Start and validate

The image's `/start.sh` supplies model, native worker and `/state` paths.

- `/start.sh --mock`: synthetic camera plus the real native model; no hardware.
- `/start.sh`: real OAK camera, with picking in dry-run mode. Gateway mutations
  are blocked as well, even when a caller supplies a valid gateway token.
- `/start.sh --live`: enables the existing confirmed robot workflow. This also
  requires `GATEWAY_ROBOT_HOST` to be explicitly configured.

Configure `GATEWAY_CAMERA_IP`, `GATEWAY_ROBOT_HOST` and the existing `GATEWAY_RWS_*`
settings through deployment configuration. The launcher never reads the checkout's
`gateway/.env`. It forces strict authentication and session-aware control.

For a browser on the lab network, append:

```sh
--host 0.0.0.0 --dashboard-origin http://LYNX_MANAGEMENT_IP:8080
```

Use the actual address. Sign in as `lynx` with the password stored in
`/state/dashboard-token`. This password is separate from `/state/gateway-token`;
the latter is private to the coordinator/API clients. Remote access retains exact
Host/Origin and CSRF checks. Use HTTP credentials only on the trusted lab network,
or place the dashboard behind a TLS proxy. Default binding is loopback only.

Before loading the model on Linux with the real camera, the source launcher
requires **48 MiB of additional memory headroom** in every finite visible cgroup
limit, including shared parents, and in host `MemAvailable`. Set
`--model-headroom-mib` to 32..128 to choose another reserve. Insufficient or
unverifiable headroom leaves the model stopped and displays a dashboard error;
hidden parent limits and missing counters are not treated as free memory. The
guard requires the visible cgroup-v2 hierarchy used by WeOS; legacy cgroup-v1
ancestry cannot be verified and is refused. The
check first attempts an optional glibc heap trim, then measures again without
assuming any amount was freed. Charged cgroup cache remains counted. This is a
startup check, not a memory reservation or proof that later scans/reconnects fit.
Mock and non-Linux launches skip it. **The guard was added after the 0.1.1 package
was built and requires a new build to reach Lynx.**

Scan first in dry-run. Check RGB/depth alignment, valid cup-base depth, selected
cup coordinates, placement evidence and the complete proposed sequence. Test
camera reconnection and repeated scans. Use `lynx-measure-resources` as documented
in the deployment guide to measure startup, reconnect, loaded idle and scan peaks
against the actual hosting limit. Keep headroom for stop/control requests. Only
then start the explicit live mode and validate picking with the operator present.

The default capture budget is 256 MiB on persistent storage. When the budget or
free-space reserve is exhausted, new captures fail rather than deleting robot
state. Archive old image/capture records while idle; preserve the recovery journal
and placement grid. `--capture-budget-mib` changes the budget. `--camera-width`
and `--camera-height` can select 640×360 later, after checking depth and picking
accuracy at that resolution.

## Quantization

INT8 execution is enabled in the NCNN build, but the current model is **not
quantized**. Its weights are about 11 MB; quantizing them cannot reduce the entire
camera/gateway/application footprint by four times. The first reductions therefore
remove the heavy inference runtime, duplicate services and large frames.

An INT8 candidate needs representative cup images for NCNN calibration and an
offline comparison of cup-base masks, valid-depth rates and resulting robot XYZ,
including difficult orientations and lighting. Use a new model directory and
manifest (`--precision int8`) for that candidate. Keep the FP32 baseline available.
No unvalidated INT8 export is silently substituted into the picking path.

## Verification performed during implementation

- Gateway regression tests and new camera, IPC, model, state, lifecycle and access
  tests passed without robot hardware.
- The native C++ worker compiled against NCNN 20260526 and minimal OpenCV on the
  development Mac. Native core tests, sanitizer checks and actual 320/640 model
  smoke checks passed. This Mac executable is not a Linux deployment binary.
- On three recorded cup images, the 320 native worker matched Ultralytics
  8.4.166 detection counts; minimum mask IoU was 0.9999604 and maximum centroid
  difference was 0.00465 pixels. One 640 comparison had identical masks.
  See [native/README.md](native/README.md) for reproducible checks. This validates
  the port, not 320-versus-640 detection recall or physical grasp accuracy.
- A combined gateway/dashboard/native-worker mock scan completed the existing
  three-frame placement assessment without importing PyTorch or Ultralytics.
  Reproduce it in a development environment with FastAPI/httpx installed:

  ```sh
  python -m lynx.tests.runtime_smoke --model "$MODEL_DIR" --worker "$NATIVE_WORKER"
  ```
- The production Linux ARM64 image built successfully. A network-disabled,
  read-only container completed a three-frame mock-camera scan with the actual
  320 model; unauthenticated gateway access and dry-run mutations were rejected.
  Native inference timing in Docker on the Mac is not a Lynx performance result.

The [target deployment check](deploy/LIVE_CHECK_2026-11-06.md) records successful
synthetic-camera scans and their Lynx memory measurements. The combined real OAK
pipeline and model, camera reconnection and physical cup picking still require
validation on the target hardware.
