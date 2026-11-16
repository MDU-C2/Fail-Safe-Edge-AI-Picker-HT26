# Build the Lynx command-line picker package

Use **`--profile cli`** for the commands-only runtime. It connects to the existing
C++ gateway on port 8000. One native C++ worker performs NCNN segmentation,
five-frame object-depth calculations and placement-grid assessment. The Python
process retains the shared motion/stop/recovery controller and uses only
`requests` plus its HTTP dependencies. It contains no UI, Python OpenCV, NumPy,
DepthAI, PyTorch, Ultralytics, FastAPI or Uvicorn. Native OpenCV core/imgproc are
statically linked into the worker.

The **0.2.5 single-pass package is deployed and passed its camera-only check**:
one observation, five fresh raw depth samples, 23 seconds including startup and
saving, and three candidates listed as available. No robot commands were sent;
placement/recovery hashes were unchanged. Only `cuppicker` was reloaded, and the
existing gateway remained healthy. No picker or inference worker remained after
the check. Physical picking was not tested. See the
[2026-11-13 CLI validation report](CLI_CHECK_2026-11-13.md) for the exact scope and
memory measurements. The old combined gateway/dashboard build is still selected by
`--profile combined` (also the preparer's default). Its historical
[2026-11-06 report](LIVE_CHECK_2026-11-06.md) does not validate the CLI profile.

## Select the model and stage an isolated context

Run from the repository root on the development machine. `MODEL_DIR` must name an
actual prepared NCNN export, not a PyTorch checkpoint. For an unprepared 640 INT8
export, generate the manifest once with the export's actual precision:

```sh
python -m lynx.prepare_model "$MODEL_DIR" \
  --input-size 640 --classes-file "$MODEL_DIR/classes.txt" --precision int8
python lynx/deploy/prepare_context.py --profile cli \
  --model-dir "$MODEL_DIR" --output /tmp/lynx-cli-0.2.5-context
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
  --tag lynx-picker-cli:0.2.5 /tmp/lynx-cli-0.2.5-context
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
`/state/cli-0.2.0`. Keep this existing state path when updating to package 0.2.5;
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
  --image lynx-picker-cli:0.2.5 --version 0.2.5 \
  --description "Lynx command-line picker with native perception and external gateway" \
  --output /tmp/lynx-picker-0.2.5.wmopkg
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

The installed 0.2.5 package supplies the single-pass defaults below. Exit any
current live CLI before replacing or reloading its application. The WeOS app's
cosmetic description still says 0.2.4; the verified package and installed-module
hashes establish that the running image is 0.2.5.

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

The default uses **one full inference pass per scan**, followed by **one mandatory
fresh verification pass before each pick**, with no automatic depth-uncertainty
retries. Each pass still collects five fresh raw depth samples for stable object
coordinates; five samples do not mean five inference passes. Placement evidence
uses the newest raw depth. The verification age limit is **60 seconds**, selectable
with `--max-result-age`; it starts when the first raw-frame capture begins,
includes inference and pre-grip motion, and is checked immediately before grip
dispatch. A failed or stale
check blocks the pick. The Mac's 15-second default and existing three-observation
profile are unchanged.

Camera-derived picking height remains off by default, with 250 mm travel TCP
height and the existing diameter + 70 mm opening allowance. Shared library speed defaults remain
250 mm/s travel and 200 mm/s Home; an explicit `--speed` uses the existing caps.
The fixed pick TCP height is 70 mm. No Mac UI speed override is imported.

Empty Enter is ignored; type `scan` to request a new camera check. The terminal
offers scan/list/show, goto/hover, batch preview and confirmation,
stop/status/reset, and supported Home proposals. `home right|left|both` previews
an action; `confirm-home <code>` confirms the operator's inspection. Unsupported
gateway capabilities remain unavailable. `home both` runs right then left. See
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
