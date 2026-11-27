# Build the Lynx command-line picker package

Use **`--profile cli`** for the commands-only runtime. It connects to the existing
C++ gateway on port 8000. One native C++ worker performs NCNN segmentation,
five-frame object-depth calculations and placement-grid assessment. The Python
process retains the shared motion/stop/recovery controller and uses only
`requests` plus its HTTP dependencies. It contains no UI, Python OpenCV, NumPy,
DepthAI, PyTorch, Ultralytics, FastAPI or Uvicorn. Native OpenCV core/imgproc are
statically linked into the worker.

**Installed package: 0.2.13.** Native Lynx supports finite camera-refreshed
batches and tertiary queue standby. Installed files and offline behavior were
verified on Lynx; the existing dashboard service is online and inactive.
Standby has not been armed, and the existing failed `left_park` queue is
preserved for inspection. See the
[2026-11-20 queue update](QUEUE_FAILOVER_UPDATE_2026-11-20.md) for launch commands,
package identity, preserved state and validation limits. The
[2026-11-19 rollback report](CAMERA_ROLLBACK_2026-11-19.md) describes the preceding
0.2.10 installation. Building alone does not replace an installed application.

The historical **0.2.10** update installed the complete smooth handover plan.
The earlier 0.2.9 package blended only some movements; its
`handover_motion=blended_path` status did not establish that the complete plan
was present. See the
[2026-11-19 smooth handover update](SMOOTH_HANDOVER_UPDATE_2026-11-19.md) for
package identity and validation scope. Installed hashes and offline behavior
were verified directly on Lynx. No live capture or robot movement was performed
for that historical update; it does not establish the current installed runtime.
The [2026-11-17 workflow update](WORKFLOW_UPDATE_2026-11-17.md) records the
historical 0.2.9 deployment. The
[2026-11-13 CLI validation report](CLI_CHECK_2026-11-13.md) records historical
0.2.5 camera and memory checks, not 0.2.10 validation.
The old combined gateway/dashboard build is still selected by
`--profile combined` (also the preparer's default). Its historical
[2026-11-06 report](LIVE_CHECK_2026-11-06.md) does not validate the CLI profile.
Both profiles use the original gateway control lease. The combined profile now
packages the gateway from `main`; fresh camera reads wait in the picker client,
without gateway session, watchdog or synchronized-frame extensions. Rebuilding
this source does not change an already installed gateway or picker package.

## Select the model and stage an isolated context

Run from the repository root on the development machine. Select a new release
version in `PICKER_VERSION`; do not reuse an installed package's version for
changed source. `MODEL_DIR` must name an
actual prepared NCNN export, not a PyTorch checkpoint. For an unprepared 640 INT8
export, generate the manifest once with the export's actual precision:

```sh
python -m lynx.prepare_model "$MODEL_DIR" \
  --input-size 640 --classes-file "$MODEL_DIR/classes.txt" --precision int8
python lynx/deploy/prepare_context.py --profile cli \
  --model-dir "$MODEL_DIR" --output "/tmp/lynx-cli-${PICKER_VERSION}-context"
```

A manifest argument does not quantize or resize an export. Keep the FP32 baseline;
the observed FP32/INT8 robot-XYZ difference reached 8.07 mm in a saved-frame Mac
comparison and needs physical validation. See [the picker guide](../README.md).

The output must be new. The CLI allowlist stages only the control/runtime modules,
native source, vendored headers/licenses, selected model and build inputs. It
excludes the gateway package, web assets, image-processing Python modules,
credentials, `.env`, calibration, grid/recovery records and captures.
`source-manifest.json` records every staged file's SHA-256. In this profile,
`Dockerfile.cli`, `start-cli.sh` and `requirements-cli-arm64.lock` are staged as
`Dockerfile`, `start.sh` and `requirements-arm64.lock` respectively.

## Build the ARM64 image

```sh
docker buildx build --platform linux/arm64 --load \
  --tag "lynx-picker-cli:${PICKER_VERSION}" "/tmp/lynx-cli-${PICKER_VERSION}-context"
```

Use the staged context, never the full checkout. An ARM64 Linux host or Docker
Desktop/buildx emulation can build it. The build runs native tests and checks that
the final Python runtime cannot import the excluded image/ML/camera/web packages.
It does not contact the camera or robot.

`Dockerfile.cli` pins and checks the NCNN 20260526 and OpenCV 4.12.0 source archives.
NCNN uses baseline ARMv8-A, INT8 support and one execution thread, without Vulkan
or OpenMP. OpenCV builds only core/imgproc, without Python bindings, GUI, video I/O,
image codecs or extra parallel backends. The final image contains the worker,
Python runtime, five hash-pinned HTTP wheels, allowlisted application source and
selected model. Compilers and build trees stay in builder stages.

The Debian base packages can change; record the tested immutable image ID and
optionally pin `PYTHON_IMAGE` to a validated digest for a release. Keep
`source-manifest.json`, `requirements-arm64.lock`, the model manifest and package
sidecar together. Dependency licenses are retained in
`/usr/share/licenses/lynx-picker/`; model/export licensing and attribution must
accompany the selected weights separately. The vendored nlohmann/json 3.12.0 MIT
license and source hashes are included. The native numerical reference and its
Ultralytics source attribution are documented in [native/README.md](../native/README.md).

## Prepare persistent state

Prepare state on the development machine with its existing calibration-validation
dependencies, using the current seven-cell profile and matching reference:

```sh
python -m lynx.prepare_state \
  --state-dir "$NEW_STATE_DIR" \
  --calibration calibration/calibration_grid.json \
  --placing-grid capture_and_detect/placing_grid_outside_tray.json \
  --table-reference capture_and_detect/placing_grid_outside_tray.table-reference.json \
  --external-gateway-token-file "$PRIVATE_GATEWAY_TOKEN_FILE"
```

The credential is the existing gateway's token in a private regular file, not a
newly generated gateway identity. Local preparation creates the copy with mode
0600 on a POSIX filesystem; no second gateway registry or dashboard token is
created. The target `/state` SD filesystem is VFAT and cannot enforce POSIX 0600.
Token access there depends on WeOS application/mount access, not `chmod`; an
application with access to the shared storage is not isolated by that mode.
Never include the token in the build
context or command line. Preparation copies occupied/reserved records unchanged
and starts new deployments with recovery required. It refuses existing state
directories. Preserve earlier state/journals; reuse the prepared directory on
updates instead of reinitializing it.

Mount persistent SD storage at `/state` and put the prepared directory at
`/state/cli-0.2.0`. Keep this existing state path when updating the package;
package version changes do not require reinitializing robot state. The CLI reference identity must match calibration, actual camera
intrinsics, seven-cell geometry, 90 mm cell size and table height exactly. The
current profile requires its calibrated 1920×1080 aligned frames; reducing camera
resolution invalidates that reference. Do not substitute the experimental
`calibration/calibration.json` for `calibration_grid.json`.

## Package for WeOS

Use a Linux/WSL host with Docker, `squashfs-tools`, `u-boot-tools`,
`device-tree-compiler` and `file`:

```sh
python lynx/deploy/package_weos.py \
  --image "lynx-picker-cli:${PICKER_VERSION}" --version "$PICKER_VERSION" \
  --description "Lynx command-line picker with native perception and external gateway" \
  --output "/tmp/lynx-picker-${PICKER_VERSION}.wmopkg"
```

On macOS/Windows with Docker Desktop, add `--container-tools`. Add
`--signing-key /path/to/key` when using the project's configured signing key.
Packaging exports a stopped ARM64 container and invokes the Westermo generator
in rootfs mode; it never starts the application. The sidecar records the package
hash and immutable source image ID. Containerized packaging has no network or
Docker socket during compression.

A package does not configure WeOS mounts or networking. Configure the persistent
`/state` mount and a network arrangement that can reach the already running
gateway. Use loopback only when the containers' network arrangement actually
exposes that gateway there. Do not start a second camera owner.

## Startup commands

The current source supplies the finite camera-refreshed defaults below. Exit any current
live CLI before replacing or reloading its application. Verify the
package version and installed-module hashes; the WeOS app's cosmetic description
alone does not establish which code is running.

Connect with `ssh admin@192.168.125.20`. At the WeOS prompt:

```text
app-hosting
show app cuppicker
```

If stopped, use `start cuppicker`, then `attach cuppicker`. The configured app
uses an idle container command; start the picker explicitly from its shell.
The existing gateway and copied token are already configured. Do not recreate
state or replace the gateway when updating the picker package.
The active WeOS configuration was saved with `copy run start`. `cuppicker` has
autostart disabled and must be started manually; a device reboot was not tested.

`/start.sh` supplies `/opt/model`, the native executable and a default `/state`
path; later command-line arguments select the deployment subdirectory.
The initial check is camera/model/grid only:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0 --scan-once
```

This saves one single-observation scan and exits. It sends no motion, does not
acknowledge recovery, and does not reconcile/release/reserve placement records.
`--scan-once` rejects `--live`.

Interactive dry run:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0
```

After validation, explicit live startup is:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0 --live
```

The native runtime takes a nonblocking `flock` on `picker-native.lock` in its
state directory before loading the worker. The CLI and managed native service
cannot load two workers using the same state directory. Process exit releases
the lock; keep its file in place. This does not exclude controllers on other
machines or using different state directories.

To arm one queued takeover batch behind cloud and Pi:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0 --live --standby
```

Standby defaults to `/state/cli-0.2.0/tertiary-token`; `--token-file` selects a
different private regular file. The token must identify the gateway's existing
`tertiary` client at priority **100**, behind cloud **300** and Pi **200**.
The state preparer does not create this identity or token. Do not include any
credential in an image, source file or command line. `--standby` requires
`--live` and rejects `--scan-once`.

After obtaining control, standby waits for the previously accepted gateway queue
to finish. It does not replay accepted work, retry an uncertain queue POST, or
automatically clear a failed or cancelled queue. Confirmed cup release and
clearance allow the existing right-via parking tail to finish if necessary before
the fresh scan. Unknown movement or gripper state requires inspection. The
Lynx lease is retained through scans, cups and final Home; loss blocks further
submissions and leaves an accepted cup to finish independently. Deploy the
updated `capture_and_detect/capture_and_detect_que.py` to the primary/cloud and
Pi runners as well: they must retain the batch lease during camera gaps and
between cups to prevent unintended takeover of a healthy primary.

Ctrl+C in standby exits without cancelling the accepted queue. Ordinary
interactive CLI Stop and quit still request the existing software stop. This is
one finite takeover batch; there is no continuous watch after it finishes.

The current native source uses **one full inference pass per scan**, with a
fresh workspace-and-grid capture after every completed cup. Each observation
collects up to five distinct raw depth samples for object coordinates and uses
the latest raw aligned frame for placement evidence. Between-cup refresh waits
for a new camera sequence. Legacy verification-age arguments remain accepted
without imposing an image-age deadline. Confirmation, reservations, Stop and
recovery checks still apply. Fresh live scans reconcile grid occupancy and
remove stale reservation markers, including after a failed handover. Unknown
visibility remains unavailable. Scanning does not clear robot recovery, and
grid bookkeeping changes do not interrupt an active handover.

The native `smooth_dual_arm_v1` plan uses **19 jobs per cup, including 12 buffered
paths**. It overlaps the left approach/grip with right preparation, then the left
handover approach with right positioning beside it. After confirmed right grip
and left release, the arms separate together; turnover waits for both separation
movements. Linear moves are used near the cup, with 10 mm intermediate blends,
a 3 mm receiving corner and exact final stops. Paths use 60% acceleration and
40% ramp, with a 1.5 m/s² carrying limit and 30% turnover acceleration.
Both grippers start and finish open. Each cup finishes placement before the next
starts; the smooth branch's simulation-only inter-cup overlap is not included.
The left returns to the existing left park target; after retracting 80 mm above
placement, the right returns to the existing right-via target before capture.
There is no Home between cups. A batch updates its pending cups and destinations
from each fresh scan. A full grid makes it wait and refresh while parked, without
Home while source cups remain. A remaining unpickable source also prevents
empty-workspace Home. Once a scan finds the workspace empty, the batch goes Home
once, right then left, and finishes. A single selection places only one cup,
refreshes the scene and returns control without automatic Home.

The CLI and combined dashboard use the enhanced shared workflow. The combined
profile's `web_app.py` imports `picker_workflow.py`, independently of the V4
capture workflow in `capture_and_detect.py`. In the repository, both entry
points share the normal smooth dual-arm plan in `handover_plan.py`. The finite
native batch policy is selected in `lynx/picker.py` and `lynx/runtime.py`.
The standalone desktop `picker_workflow.py` terminal keeps its snapshot policy,
and `capture_and_detect.py` retains its existing workflow. The pre-update 0.2.10
deployment used saved-scan picking in the native CLI and managed service; its
historical reports do not validate the new source behavior.

Camera-derived picking height remains off by default. Dual-arm travel TCP height
is 250 mm (409 mm at the flange); left-only travel remains 250 mm. The existing
diameter + 70 mm opening allowance is retained. Shared library speed defaults remain
250 mm/s travel and 200 mm/s Home; an explicit `--speed` uses the existing caps.
The fixed pick TCP height is 70 mm. No Mac UI speed override is imported.

Empty Enter is ignored; type `scan` to request a new camera check. The terminal
offers scan/list/show, goto/hover, batch preview and confirmation,
stop/status/reset, and supported Home proposals. `home right|left|both` previews
an action; `confirm-home <code>` confirms the operator's inspection. Gateway
discovery is informational; the actual operation still reports unsupported
commands or invalid controller state. `home both` runs right then left. See
[the picker guide](../README.md) for the complete command table and recovery rules.

## Measure before live use

Linux startup requires 128 MiB of additional verified host/shared-cgroup headroom
before model loading. Only a measured shortfall is retried, for **up to five
seconds** with 0.2-second polling; the model remains unstarted until a check
passes. Unknown/hidden ancestry or unreadable counters fail immediately. The
128 MiB requirement is never lowered. This is a snapshot, not a reservation:
measure startup, loaded idle, repeated dry-run
scans and reconnection using the actual gateway and model together.

The included read-only profiler can run inside the application container:

```sh
lynx-measure-resources --pid 1 --samples 120 --interval 1 \
  --output /state/cli-0.2.0/resources-dry-run-01.json
```

Use the actual Python launcher PID if a supervisor is present. It records the
process tree and cgroup counters, not tokens, command lines or environment
variables, and never starts a scan or robot action. Compare shared parent
`memory.current`/`memory.peak` with its real limit; summing process RSS does not
capture charged cache or other applications. Inspect memory events and retain
headroom for control/stop handling.

The deployed **0.2.5 package** completed its one-observation camera check in
**23 s**, including startup and saving. Sampled shared peak usage was
**228,663,296 bytes (218.07 MiB)** against a 254,283,776-byte limit; the picker
sampled peak was **163,155,968 bytes (155.60 MiB)**. Shared `max` stayed at its
pre-existing 827, with `oom=0` and `oom_kill=0`. The unchanged 128 MiB startup
guard passed at **128.8 MiB** visible shared headroom. Placement/recovery hashes
were unchanged, and the existing gateway remained healthy with no active control
lease or move after the check.

The report keeps the historical 0.2.4 three-observation 43/41-second checks,
earlier 0.2.2 overlay and 117.0–121.7 MiB startup refusals separately. The two
0.2.4 launches restarted only the picker process inside the same container.
Sampled measurements do not establish long-running reliability or worst-case
memory use; no physical picking was tested in these camera checks.
The earlier standalone Mac grid result (266 saved
frames, 47.61 MiB peak RSS) excludes NCNN, Python and gateway memory. Store
captures on persistent storage; archive only while idle and preserve placement
and recovery records.
