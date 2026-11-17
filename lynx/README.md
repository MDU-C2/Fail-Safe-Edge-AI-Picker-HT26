# Run the command-line cup picker on Lynx

This managed CLI belongs to the enhanced UI/shared-controller workflow. It is
separate from the restored `capture_and_detect/capture_and_detect.py` V4 terminal.
The combined browser profile uses `web_app.py` and `picker_workflow.py`; it does
not import or modify the original V4 terminal. See the
[entry-point guide](../capture_and_detect/README.md).

The `cli` profile uses the **existing C++ gateway on port 8000** and adds a
command-line picker. The gateway remains the sole OAK camera and robot connection
owner. One persistent C++ process runs NCNN segmentation, object-depth geometry
and placement-grid assessment. A small Python process retains the shared
selection, motion sequencing, stop, confirmation, placement records and recovery
logic; its only third-party Python dependency is `requests` and its dependencies.

This profile has no browser UI, Python OpenCV, NumPy, DepthAI, PyTorch,
Ultralytics, FastAPI or Ollama. Native OpenCV **core and imgproc** are still linked
into the C++ worker. The Mac dashboard stays on the Mac; do not operate two live
controllers against the robot concurrently.

**Release status:** **0.2.9 is installed and the Lynx service is ready** with the
V4 snapshot workflow, smooth handover and no automatic Home. Installed hashes and
preservation of calibration, placement state and recovery were verified. Offline replay passed;
no live scan or physical picking was performed for this update. See the
[2026-11-17 workflow update](deploy/WORKFLOW_UPDATE_2026-11-17.md) for package
identity and validation scope. The
[2026-11-13 CLI validation report](deploy/CLI_CHECK_2026-11-13.md) records historical
0.2.5 camera and memory checks; it does not validate physical picking or 0.2.9.
The older combined
Python gateway/dashboard profile remains available as `--profile combined`;
its [2026-11-06 measurements](deploy/LIVE_CHECK_2026-11-06.md) concern that older
profile, not this command-line runtime.

## Prepare the model and persistent state

Run preparation from the repository root on the development machine, using its
existing image-processing environment. Those preparation dependencies are not
installed in the command-line runtime. Build and packaging steps are in
[deploy/README.md](deploy/README.md).

Select an actual NCNN export with `model.ncnn.param`, `model.ncnn.bin`,
`metadata.yaml` and classes in their export index order. For a **640 INT8 export**
that does not already have its validated manifest:

```sh
python -m lynx.prepare_model "$MODEL_DIR" \
  --input-size 640 --classes-file "$MODEL_DIR/classes.txt" --precision int8
```

Use the export's real resolution and precision. A manifest flag does not resize
or quantize a model. Existing manifests and parameter/weight hashes are checked
at startup; preparation refuses to overwrite an existing manifest.

The current placement profile contains seven 90 mm cells: **P2, P3, P4, P5,
P8, P9 and P10**, outside the tray. It uses `calibration_grid.json` and its matching
frozen empty-table reference. Keep those exact three artifacts together; the
experimental `calibration.json` has a different Z offset and is not a substitute.
The current reference is bound to the calibrated **1920×1080** intrinsics, layout,
cell size, table height and calibration digest. Changing camera resolution or
moving a cell requires a separately validated matching reference.

Create a new state directory from the currently inspected deployment state:

```sh
python -m lynx.prepare_state \
  --state-dir "$NEW_STATE_DIR" \
  --calibration calibration/calibration_grid.json \
  --placing-grid capture_and_detect/placing_grid_outside_tray.json \
  --table-reference capture_and_detect/placing_grid_outside_tray.table-reference.json \
  --external-gateway-token-file "$PRIVATE_GATEWAY_TOKEN_FILE"
```

The token file must contain the **existing gateway's token**, be a private regular
file (for example mode 0600), and not be a symlink. Do not put the token in a command
line, source file or image. Local preparation creates `gateway-token` with mode
0600 on a POSIX filesystem; it does not create a second gateway registry or
dashboard password. The actual Lynx `/state` SD filesystem is **VFAT**, which
cannot enforce POSIX 0600 permissions. On that target, access follows the WeOS
application/mount access configuration; `chmod` does not isolate the token from
other applications with access to the same storage.

Preparation validates the reference before writing, copies calibration/grid and
reference bytes unchanged, and preserves occupied/reserved placement records.
A new state directory starts with recovery required. It does not certify that a
previous host's movement finished. Preserve the old state and recovery history;
after inspecting the robot, use the explicit recovery procedure. Reuse the same
state directory for later restarts; preparation refuses an existing directory.
Mount persistent SD storage at `/state`, with this deployment under
`/state/cli-0.2.0`. Package updates reuse this path; a package update must not
reinitialize or discard existing state. Keep captures, grid and recovery journal off the RAM-backed
container filesystem.

## Start and use the picker

Installed package 0.2.9 uses the snapshot defaults described below. Do not
replace or restart an application while an operator is using its live CLI.

Connect to the configured Lynx application:

```sh
ssh admin@192.168.125.20
```

At the WeOS prompt, enter `app-hosting`, then `show app cuppicker`. If the app
is stopped, run `start cuppicker`; then run `attach cuppicker`. The configured
application stays idle until you start the picker below. Existing credentials
and persistent state are already copied; no new token approval is pending.
The WeOS configuration was saved with `copy run start`. The picker has autostart
disabled and must be started manually; a device reboot has not been tested.

Inside the prepared application container, `/start.sh` supplies the packaged
model and worker. The gateway must already be running and reachable through the
container's configured network; loopback only reaches it when that network
arrangement exposes the gateway there.

Camera/model/grid check, saving one single-observation scan and then exiting:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0 --scan-once
```

`--scan-once` sends no motion and **does not reconcile, release or reserve any
placement records**. It saves capture/audit files. It cannot be combined with
`--live` and does not acknowledge recovery.

Interactive dry run:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0
```

Explicit live operation, after checking the saved images, coordinates and robot:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0 --live
```

Startup itself performs no robot movement. The current single-operator source
does not acquire OS file locks. Device handoff and recovery guards still apply.
It does not prevent a separate Mac
controller from connecting, so close that controller before live use.

| Command | Result |
| --- | --- |
| `scan` | Capture and assess the scene once; no movement |
| Empty Enter | Ignore; no camera check or movement |
| `list`, `show 2` | Inspect cups in the current capture |
| `goto 2` | Pick cup 2 using the saved scan, hand it over and place it; finish above the destination |
| `goto all` | Preview the available batch; `confirm <code>` starts it |
| `start` | Scan and preview a batch; still requires confirmation |
| `hover 2` | Approach above cup 2 using its saved scan coordinates |
| `status`, `robot status` | Application state, or refreshed gateway/controller state |
| `home right`, `home left`, `home both` | Preview a supported Home action |
| `confirm-home <code>` | Confirm the inspected return path and execute that Home proposal |
| `stop` or `halt` | Latch the stop and request the gateway's software stop |
| `cancel` | Discard a proposal; does not stop dispatched motion |
| `reset` | Acknowledge recovery after inspection; sends no movement |
| `quit` | Exit; active work receives a software-stop request, with no automatic home/resume |

Gateway discovery is informational. Implemented Home controls are offered with
their existing confirmation and execution checks; unsupported operations can
still fail when requested. `home both` returns the right arm first, then the left. A
legacy gateway may need the existing read-only `--controller-profile` for the
required status checks; adding that profile does not create missing gateway
endpoints or weaken the guarded-restart fence. Software stop still depends on
the gateway/controller; use the physical stop when immediate stopping is needed.

## Preserved motion and camera checks

The CLI uses the shared controller and plans, including their smooth handover
paths, overlapping arm movements, completion checks, reservations and recovery
journal. Each completed pick ends above the destination; Home remains a separate
operator command. Defaults are:

| Setting | Value |
| --- | --- |
| Travel TCP height | 250 mm; the 159 mm gripper offset makes the flange target 409 mm |
| Fixed picking TCP height | 70 mm |
| Opening allowance | Measured base diameter + 70 mm, through the existing gripper conversion/checks |
| Ordinary travel speed | 250 mm/s |
| Home speed when requested | 200 mm/s, subject to an explicitly selected lower speed cap |
| Camera-derived picking height | Off; `--camera-z` explicitly selects the existing measured-height policy |
| Object-depth samples | Up to five distinct frames; `--depth-frames` accepts 1..9 |
| Normal scan | One full YOLO/perception pass |
| Placement evidence | The saved scan; no capture before or between picks |
| Observation expiry | None in the snapshot workflow; `--max-result-age` is retained for argument compatibility |

`--speed` selects a different travel speed through the existing plan limits. An
explicit setting previously used in the Mac UI is not automatically imported.
With `--camera-z`, the pick TCP uses the existing base Z + 20 mm policy with a
40 mm floor. Object depth uses the V4 mask-depth rule and available positive
samples; the removed core/spread rejection does not block cup selection.
Placement evidence and saved depth use the **latest raw aligned frame**.
**Five raw depth samples do not mean five inference passes.** A scan performs
one observation. If no new camera sequence arrives, available samples may be
used. The initial saved destination evidence must be clear, and its reservation
must still be valid before motion.

There is no automatic recapture, cup rematching or observation-age deadline
before or between picks. Completed cups become unavailable; untouched cups stay
selectable from the same scan. Scan again when the physical scene changes.
Confirmation expiry, Stop, lease/session checks, placement reservations and the
recovery journal remain. These settings are verified in the running 0.2.9
service. The 0.2.5 report records earlier deployed behavior.

Native grid assessment preserves cup-to-cell ownership, connected depth groups,
cup-edge uncertainty, visibility and other-object checks. Oversized cup masks or
group crops above 262,144 pixels, or aggregate decoded cup masks above 8,388,608
pixels, fail the assessment before unbounded geometry allocation. They never
silently clear a destination.

On Linux, startup requires 128 MiB of additional verified headroom from the host
and visible shared cgroup hierarchy before loading the model. A measured
shortfall is rechecked for **up to five seconds**, at 0.2-second intervals; the
128 MiB requirement is unchanged and no model starts during that wait. Unknown
or unreadable memory accounting fails immediately. A passing snapshot does
not reserve memory or prove that later camera/model peaks fit. Keep capture files
on persistent storage and monitor available disk space; archive them only while
idle, preserving placement and recovery records.

The deployed 0.2.5 camera-only check took **23 s**, including startup and saving.
Sampled shared usage reached **218.07 MiB** against a **242.50 MiB** limit; the
picker application sampled peak was **155.60 MiB**. No memory-event counter
increased. The unchanged 128 MiB startup guard passed at **128.8 MiB** visible
shared headroom. These are one run's samples, not worst-case bounds or physical
picking validation. The report retains the historical 0.2.4 three-observation
43/41-second results separately; its second run restarted only the picker
process inside the same container.

## Model sensitivity and validation

INT8 reduces model size and can reduce inference memory, but it does not shrink
the gateway, raw camera frames or all application memory by the same factor.
In the saved-frame Mac comparison, matched FP32/INT8 observations differed by up
to **8.07 mm in Euclidean robot XYZ**. Small segmentation changes can select
different depth pixels, so mask agreement alone does not prove grasp agreement.
That comparison is not physical picking validation or a general accuracy bound.
Keep the FP32 baseline and check the actual cup-base depths and proposed XYZ
before adopting the INT8 candidate live.

The native placement implementation matched every returned slot/evidence field
on 266 saved full-resolution frames under the seven-cell profile. The final
standalone Mac replay had a 0.164 s median and 47.61 MiB peak RSS. Synthetic
placement tests and helper-level ownership/shared-pixel comparisons also passed.
These measurements exclude the loaded NCNN model, Python controller and gateway;
only combined target measurements can establish deployment headroom.

## Dependencies and attribution

The command-line image keeps the NCNN, OpenCV and nlohmann/json license texts in
`/usr/share/licenses/lynx-picker/`; nlohmann/json 3.12.0 is vendored with its MIT
license and [source/hash record](native/vendor/nlohmann/README.md). The Python
HTTP dependencies retain their wheel metadata/licenses. Build inputs and runtime
wheel hashes are recorded in the staged source manifest and lock file.

The native postprocessing reference is Ultralytics 8.4.166, documented with source
links in [native/README.md](native/README.md). Preserve the selected model's
training/export provenance, license and attribution with the release. Dependency
license files do not establish a license for model weights; the context preparer
copies only the explicit model files listed in its manifest contract.
