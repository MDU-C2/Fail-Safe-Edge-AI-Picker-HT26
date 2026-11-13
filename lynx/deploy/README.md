# Lynx image and WeOS package

This image runs the gateway and cup-picking coordinator in one Python process on
port 8080, with a separate camera-free native NCNN worker. The gateway owns the
OAK camera. The Python runtime has no PyTorch, Ultralytics, NCNN Python bindings,
GUI libraries, or Uvicorn standard extras. NCNN uses baseline ARMv8-A instructions
and a single execution thread, with INT8 support compiled in. Enabling that support
does not quantize an FP32 model.

The initial camera profile is 960x540 at 2 FPS. Use a properly exported 320 model;
changing the input-size argument of the repository's 640 export is insufficient.
640x360 camera output is an optional later reduction after checking RGB/depth
alignment and grasp accuracy. Camera size and model input size are independent.
Use `--camera-width`, `--camera-height` and `--camera-fps` launcher arguments to
change that profile; these arguments take precedence over gateway environment
settings in the combined application.

## Prepare the model and a clean context

Run from the repository root, with Python 3.11 or later. Set `MODEL_DIR` to the
directory containing the selected NCNN export, not a PyTorch checkpoint.

```sh
python -m lynx.prepare_model "$MODEL_DIR" \
  --input-size 320 --classes-file "$MODEL_DIR/classes.txt" --precision fp32
python lynx/deploy/prepare_context.py \
  --model-dir "$MODEL_DIR" --output /tmp/lynx-picker-context
```

The context must be a new directory. Only Python source, web assets, native source,
the prepared model and build inputs are copied. Existing credentials, `.env`,
camera calibration, placement/recovery state, captures and training checkpoints
are excluded. `source-manifest.json` records the SHA-256 of every staged file.

The local investigation recovered a 320 FP32 model from the existing
`envoy-failsafeseg320fix-1.0.wmopkg`. Its binary SHA-256 is
`408da3a087447cf2d768cf57c20ff9a9b91f7916896ba0c1092ee291f54d5821` and its parameter
SHA-256 is `1d3462f0f84353ced7a9743b3dab96fcf8372ec8d38810f52558ceab71be2664`.
These identify an available baseline, not a hardware-validated release.

## Build on a machine with Docker

```sh
docker buildx build --platform linux/arm64 --load \
  --tag lynx-picker:0.1.0 /tmp/lynx-picker-context
```

An ARM64 Linux build host is fastest; Docker Desktop or buildx emulation can also
build ARM64. The native build runs its dependency-free tests and the final image
checks native/Python imports. No camera connection or robot command runs during
the image build.

The NCNN source release is 20260526 with an archive checksum. All 23 runtime Python
wheels are pinned and hash-checked in `requirements-arm64.lock`, including DepthAI
3.1.0, NumPy 1.26.4 and OpenCV headless 4.11.0.86. Those ARM64 CPython 3.11 wheels
were resolved and downloaded successfully. DepthAI's OAK pipeline still needs a
real-camera test. The Debian packages and `python:3.11-slim-bookworm` base receive
updates; freeze the tested image ID, and optionally supply `--build-arg
PYTHON_IMAGE=python@sha256:...` when recording a release.

The builder includes CMake and compilers. The final image contains only the native
binary with static OpenCV core/imgproc, Python runtime dependencies,
application source and selected model. Native DepthAI is not linked into the
worker, so the camera SDK runs in only one process.

The native OpenCV 4.12.0 archive is checksum-pinned and built with GUI, video,
OpenCL, BLAS, and additional thread backends disabled. Debian's prebuilt
core/imgproc packages are deliberately avoided because they also install
Mesa/X11/LLVM dependencies.

## Persistent state and startup

Follow the state preparation instructions in [the Lynx guide](../README.md). Use
the real calibrated transform and consciously reconciled placement state; the
image contains neither. Mount that prepared directory at `/state` from persistent
SD storage. On WeOS, writes outside configured persistent mounts can consume the
RAM-backed writable layer and disappear after restart. Keep logs bounded and do
not retain captures there.

The startup command is `/start.sh`. It supplies `--state-dir /state`,
`--model /opt/model` and `--worker /usr/local/bin/lynx-ncnn-worker` to the launcher.
Additional arguments are forwarded unchanged:

- `--mock` selects a hardware-free startup.
- With no `--live`, robot movement is dry-run.
- `--live` explicitly enables the real picking path; use only after calibration,
  placement state and the dry-run target coordinates have been checked.
- `--host` defaults to `127.0.0.1`; port defaults to 8080. A non-loopback bind also
  needs `--dashboard-origin http://LYNX_MANAGEMENT_IP:8080` using the actual address.

Use the dashboard credentials prepared in `/state`, and the separate gateway
credentials for `/api/v1`. Strict gateway authentication and lease-session checks
are retained. Use a trusted lab network or an authenticated TLS proxy when exposing
the HTTP dashboard; do not publish its port to the internet.

`runtime.env.example` contains resource defaults without credentials. Network and
robot credentials are deployment settings, not image content. The startup script
repeats essential resource/path defaults because a WeOS rootfs package does not
retain Docker environment or entrypoint metadata. It runs one server worker.

The current source includes a real-camera model-start guard, added after package
0.1.1. It attempts optional glibc heap trimming and then requires 48 MiB additional
headroom from both host `MemAvailable` and all finite visible cgroup limits. The
unlimited application leaf is acceptable when the shared `/apps` limit is visible;
hidden ancestors or missing required measurements refuse model initialization.
The guard requires cgroup v2; it refuses unverifiable legacy v1 ancestry.
`--model-headroom-mib` accepts 32..128. The check does not subtract charged cache
or assume it will be reclaimed, and does not reserve memory against later growth.
Rebuild before relying on this guard; the measured packages do not contain it.

## Create a .wmopkg on Linux or WSL

Install Docker, `squashfs-tools`, `u-boot-tools`, `device-tree-compiler` and `file`, and make the built ARM64
image available to that Docker daemon. Then run from this checkout:

```sh
python lynx/deploy/package_weos.py \
  --image lynx-picker:0.1.0 --version 0.1.0 \
  --output /tmp/lynx-picker-0.1.0.wmopkg
```

Add `--signing-key /path/to/key` when signing with the project's configured key.
The helper checks the image architecture, creates a stopped container, exports its
merged filesystem, and calls the existing Westermo generator in `rootfs` mode.
It never starts the container. This avoids the old generator's incomplete OCI
layer/manifest handling. A sidecar JSON records the immutable Docker image ID and
startup/mount settings.

On macOS/Windows with Docker Desktop, add `--container-tools` to the same command.
The helper builds a small Linux packaging image and mounts only the exported
filesystem, vendor generator and optional signing key. Its packaging run has no
network and no Docker socket. Compression uses two threads and at most 256 MiB
of configured compression buffers. The production application container is still
never started by packaging.

Configure WeOS application hosting with command `/start.sh`, the `/state` SD mount,
network access to the OAK and robot, and the required launcher arguments. A package
file alone does not set up the persistent mount or application network settings.
The initial command must omit `--live`.

## Validation still needed on Lynx

Version 0.1.0 was deployed on Lynx on 2026-11-06. Three synthetic-camera scans
passed with the actual native model, while real OAK startup failed because the
camera was unavailable. The [dated deployment check](LIVE_CHECK_2026-11-06.md)
records the package identity, target limits, measured memory and timings. The
combined real-camera/model memory peak and physical picking remain unvalidated.

From a shell inside the running Lynx application container, record two minutes:

```sh
lynx-measure-resources --pid 1 --samples 120 --interval 1 \
  --output /state/resources-dry-run-01.json
```

PID 1 is the Python launcher when WeOS invokes `/start.sh` as the container
entrypoint; use the actual launcher PID if a supervisor is present. Allow an idle
baseline, perform repeated **dry-run scans** through the dashboard, and allow an
idle tail. The profiler does not initiate scans or any robot actions. Run a second
recording covering camera reconnection and compare it with the baseline. Samples
are bounded (at most 3600); output files must be new, and Ctrl+C preserves samples
already collected.

The JSON includes each process and descendant's RSS, PSS where readable, private
memory, CPU ticks, and memory high-water mark. The primary capacity comparison is
the observed cgroup usage against its actual `memory.max` (v2) or
`memory.limit_in_bytes` (v1), together with visible ancestor limits. It includes
kernel peak memory and OOM/failure counters. **Do not add process RSS and call it
total memory:** shared pages are counted repeatedly. PSS apportions those pages;
cgroup usage also accounts for cache/kernel memory and other members. A kernel
peak may predate the recording, so also inspect the peak of sampled usage.

If automatic cgroup discovery cannot see the WeOS mount, pass
`--cgroup-dir /ACTUAL/MEMORY/CGROUP` after identifying it in that container. Missing
counters and unlimited/hidden parent limits remain explicitly unavailable. The
profiler does not derive a number from the WeOS 50% setting or assume a 240 MiB
budget; the shared hosting allowance must be confirmed from observed limits and
WeOS configuration when not visible inside the container.

Building a package is not evidence that the combined process fits the hosting
memory allowance. Measure camera startup/reconnect peaks, loaded idle memory,
repeated scan peaks and control responsiveness while inference runs. First test
the real camera in dry-run, verify target coordinates against calibration, then
validate picking deliberately. Compare long-run memory and RSS at the same frame
size and scene before accepting an INT8 model or a smaller camera profile.

The Linux ARM64 image was built successfully and its native tests/import checks
passed. A network-disabled, read-only Docker run completed a synthetic-camera
three-frame scan with the actual 320 model, checked gateway authentication and
dry-run mutation blocking, and recorded process/cgroup memory with this profiler.
The worker links only standard C/C++ runtime libraries. Those timings and memory
measurements come from Docker on the development Mac with a synthetic camera;
they do not establish real Lynx speed or OAK memory requirements. Subsequent
[checks on Lynx](LIVE_CHECK_2026-11-06.md) measured synthetic scans on the actual
device. No robot movement was performed in either set of checks.

An unsigned `.wmopkg` was also generated with the containerized Linux tools. Its
embedded SquashFS was read back to verify the application/startup/model hashes,
AArch64 native executable, and absence of deployment credentials. Use the sidecar
JSON for the package hash and exact source image ID. A signing key can be supplied
when building a package for a WeOS installation that requires signatures.
