# Manual computer / Pi / Lynx dashboard

The dashboard runs on your MacBook or Windows PC. **Processing device → Use device**
selects where both the detection model and the cup-picking controller run.
The local computer and Pi use the
Python perception runtime; Lynx uses the native C++ perception runtime and the
shared Python motion controller. All three use the existing robot/camera gateway.
This is separate from the older inference-only automatic failover feature.

The generic commands below are setup examples. For the verified installation,
see [the deployment check and startup commands](DEVICE_CHECK_2026-11-13.md). Replace
the example repository, model and state paths and the `192.0.2.*` documentation
addresses with the verified installation values.

## Monitor from Windows while the Mac keeps controlling

Run the full UI with `--monitor` for a second, read-only dashboard. It keeps every
tab and reads camera, robot pose, ownership and queue status directly through the
Windows Ethernet connection. It does not acquire ownership or start a picker.
See [full Windows monitoring setup](../diagnostics/OBSERVER.md#windows-the-full-ui-alongside-the-mac)
for commands, diagnostics credentials and the limits of shared worker data.
The transfer procedure below is only for moving the **controlling** dashboard.

## Move the dashboard between Mac and Windows

The local option is **Windows PC** on Windows and **MacBook** on macOS when the
configuration uses `backend: "local"`. It loads that computer's `best.pt` and
runs detection and picking there. Raspberry Pi and Lynx remain remote choices.
The internal device ID `mac` is retained for compatibility with deployed services;
it does not send Windows processing to the Mac. A remote HTTP entry named `mac`
still means the separate Mac service.

A Git clone includes the program, model and reference files, but excludes your
private device-service tokens and dashboard ownership record. Therefore
`python web_app.py --gateway ...` runs a local-only dashboard. To get the three
device choices, transfer your existing private setup and use `--device-config`.
Do not create a new owner or delete records to attach to existing services.

Before transferring, finish all robot work, select **Pi or Lynx**, wait for Ready,
and close the source dashboard. Keep the selected remote picker service running.
From the source checkout's `capture_and_detect` directory:

```sh
python dashboard_setup.py export --config "/path/to/current/devices-combined.json" --output "/private/path/dashboard-transfer.zip"
```

Export reads status only. It requires one selected, idle remote owner, an inactive
local picker, matching identities and no recovery/reservations or unfinished
handoff. Custom local runtime records must match their saved handoff binding;
if standalone edits changed them, reconcile using the source dashboard before
exporting. It preserves calibration and reference bytes, service credentials,
controller profile, owner generations and local placement records. It excludes
model weights, capture history, saved-cup photos and Assistant keys. Keep the ZIP
private: it contains device and gateway credentials. Do not put it in Git.

Copy the ZIP to the other computer. In Windows PowerShell, substitute your actual
clone and downloaded ZIP paths:

```powershell
cd "C:\path\to\Fail-Safe-Edge-AI-Picker-HT26"
git switch vision_detection3
git pull --ff-only
cd capture_and_detect
python -m pip install -r requirements-inference.txt
python dashboard_setup.py install --bundle "$HOME\Downloads\dashboard-transfer.zip" --directory "$HOME\cup-dashboard"
python web_app.py --device-config "$HOME\cup-dashboard\devices.json" --live --port 8087
```

The install directory must be new; existing state is never overwritten. The
installer points the local model at this checkout's `best.pt`, replacing Mac
paths, and retains the currently selected remote owner. Open
`http://localhost:8087`, choose **Windows PC**, then **Use device**. The ordinary
handoff transfers the remote picker's current placement records before Windows
loads its model. Installation and dashboard startup send no robot movement.
A remote service whose network address changed needs its URL updated in the
private config. All services and the dashboard must agree on live/dry-run mode.

To return to the Mac later, select Pi or Lynx on Windows, wait for Ready, close
the Windows dashboard and **export a new bundle from its current config**.
Install it into a new private directory on the Mac and launch `web_app.py` with
that new `devices.json`. It will offer MacBook processing automatically. Reusing
an older bundle or the older Mac launcher after a transfer can have stale ownership
generations; use the newest exported config. Run only one controlling dashboard
at a time; a separate `--monitor` dashboard can remain open.
Existing saved-cup libraries and Assistant settings stay on their original host;
the minimal transfer does not migrate them. API keys can be entered per session
in Settings on the new host. On Windows, the Assistant uses session-only keys;
Remember is unavailable, so enter the key again after restarting the dashboard.
The Mac keeps its existing optional private key storage.

The ordinary terminal is separate from this managed dashboard:

```powershell
$env:GATEWAY_TOKEN = "YOUR_ACTUAL_GATEWAY_TOKEN"
python capture_and_detect.py --gateway http://192.168.125.20:8000
```

This keeps the V4 terminal capture workflow: Enter captures and processes cups;
`q` quits. Normal dual-arm picks use the same smooth handover plan as the UI,
without Home between cups. After a successful batch or a full grid, the terminal
homes once, left then right; a failed pick skips that return. The UI does not
home automatically. The original `main` gateway supports `/api/v1/robot/rws/path`,
and the controller must have `RunPath` loaded. `--left-only` keeps its original
pick/lift/replace motion and only homes the left arm at batch end.
It has no `--dry-run`, `--commands`, Assistant, managed-device ownership or UI
recovery/reservation workflow. It uses `Calibration/calibration.json` and
`placing_grid.json`, not the managed dashboard's state. Add
`--output Data/TerminalDetections` to keep its capture files separate from the UI.
Do not run it while a managed picker is operating. Reconcile the physical scene
and the next controller's destination layout before switching.

The optional enhanced scan/goto/confirm shell is a separate command:

```sh
python picker_workflow.py --commands --gateway http://192.168.125.20:8000 --dry-run
```

The browser and managed-device backends import `picker_workflow.py`; they do not
import the V4 terminal capture workflow. Both share the normal dual-arm motion
plan in `handover_plan.py`. See [entry points](README.md).

## Prepare matching persistent state

Each service needs its own persistent state directory containing:

- `calibration.json`: the validated grid calibration, copied from
  `calibration/calibration_grid.json` for the current profile.
- `placing-grid.json` and `placing-grid.table-reference.json`: the paired seven-cell
  layout and frozen empty-table reference. Preserve occupied/reserved records.
- `gateway-token`: the existing gateway credential; this is separate from the
  device-service access token below.
- `captures/`, including any existing `robot_recovery.json` and capture history.

Home, grippers, status and software Stop use the original gateway API. No
`controller-status.json` or direct ABB credentials are needed. Existing profile
files may remain in persistent state, but they do not enable reset, start or
collision controls. App recovery continues to be a local acknowledgement that
sends no robot command.

All devices must use the same physical robot and camera, the same `--robot-id`,
matching calibration/reference bytes, and identical grid IDs, coordinates and
cell size. The current profile uses P2–P5 and P8–P10 with 90 mm cells. Keep the
reference bound to the actual camera intrinsics and resolution; do not substitute
the experimental calibration or regenerate a reference from an occupied scene.

Use the existing [state preparation procedure](../lynx/README.md) for a new
directory. Preparation preserves the supplied grid and starts with recovery
required. Inspect the robot and use the existing operator recovery workflow to
resolve that state; do not edit recovery records to make a target available.
An incoming handoff refuses a target with outstanding recovery, reservations or
an interrupted state transfer. Do not reinitialize an existing deployment.

Back up the existing state before first migration, while its controller is
stopped. Keep the original state and journal. The selected source's occupancy,
clean recovery record, normal/home speed, camera-height choice transfer during a successful switch. The target saves its previous grid
and journal in `device-state-before-handoff.json`. Captures and saved-cup libraries
remain on their respective devices; scan again after every switch.

## Private dashboard configuration

Keep configuration, tokens and the dashboard ownership file outside the repository
and deployment image. For example, create a private directory
`$HOME/.config/cup-picker` on the Mac and save `devices.json` there:

```json
{
  "schema_version": 1,
  "initial_device": "lynx",
  "state_file": "dashboard-owner.json",
  "devices": {
    "mac": {
      "url": "http://127.0.0.1:8091",
      "token_file": "mac-service.token"
    },
    "pi": {
      "url": "http://192.0.2.50:8091",
      "token_file": "pi-service.token"
    },
    "lynx": {
      "url": "http://192.0.2.20:8091",
      "token_file": "lynx-service.token"
    }
  }
}
```

All three device entries are required. URLs are service origins: no embedded
credentials, path, query or fragment. File paths are relative to `devices.json`
unless absolute. Each service token file contains only its corresponding token
and an optional final newline. Example **dummy content, never for deployment**:

```text
EXAMPLE_ONLY_REPLACE_WITH_A_UNIQUE_RANDOM_TOKEN
```

Use a different random token per service, 32–512 printable ASCII characters with
no embedded whitespace. Securely place a matching copy on that device. On POSIX
storage, use mode 0700 for the private directory and 0600 for token files; do not
pass token values as command-line arguments. Lynx SD/VFAT storage cannot enforce
POSIX file modes, so restrict access through the application and mount setup.
The service uses bearer authentication; expose it only on the intended trusted
network or through a protected tunnel. It does not provide a browser UI.

`initial_device: "lynx"` bootstraps Lynx as the initial authority only when the
dashboard has no ownership file. Later starts reconcile the persisted owner;
changing this setting does not override that owner. Preserve
`dashboard-owner.json`, each device's `device-owner.json`, and handoff files.
Deleting them is not a way to unlock or reset the robot.

## Combined dashboard with Astra on the Mac

To keep the Assistant and saved-cup matching alongside the device selector,
run the Mac picker inside the dashboard. Replace only the `mac` entry above:

```json
"mac": {
  "backend": "local",
  "state_dir": "/existing/private/mac-state",
  "model": "/path/to/cup-model.pt",
  "gateway": "http://192.0.2.20:8000",
  "robot_id": "picker-lab-robot",
  "max_result_age": 30,
  "runtime_paths": {
    "placing_grid": "/existing/capture_and_detect/placing_grid_outside_tray.json",
    "output": "/existing/capture_and_detect/Data/Detections",
    "calibration": "/existing/calibration/calibration_grid.json",
    "table_reference": "/existing/capture_and_detect/placing_grid_outside_tray.table-reference.json"
  }
}
```

Keep the existing Pi/Lynx entries and `state_file`. The optional `runtime_paths`
reuses an existing direct Mac dashboard's grid, captures, recovery journal and
saved-cup library. Without it, the standard files in `state_dir` are used.
The table-reference filename must correspond to the placement-grid filename.
All devices still need matching geometry, calibration and table reference.
The local computer uses one saved snapshot without per-cup captures or image-age
expiry. Updated remote services use the same snapshot workflow. `GATEWAY_TOKEN` supplies the local computer credential when
set; otherwise the existing state-directory token is used.

Close the old direct dashboard and separate Mac service before starting this
configuration. This single-operator version no longer acquires OS file locks;
the shared JSON stores retain in-process thread synchronization. Use a separate dashboard
`--output` directory; when omitted, managed mode uses `dashboard-cache` next to
the configuration. The Pi and Lynx services continue to run separately.

Pass `--assistant-settings /private/path/assistant-settings.json` to the dashboard
to reuse Astra settings. Astra and its credentials remain in the Mac process.
**Assistant** and saved-cup matching are enabled only while the local Mac picker
owns control and is ready. Pi and Lynx retain their ordinary picking controls.
Switching devices invalidates old Assistant responses and previews; returning to
Mac preserves the saved-cup library. See [Assistant behavior](ASSISTANT.md) for
automatic camera-only refresh and the single pick confirmation.

On first attachment to existing Mac paths, an incoming handoff preserves occupied
cells from both the selected source and the Mac records. A later standalone change
to Mac occupancy triggers the same conservative merge. No occupied cell is freed
by this adoption step. Recovery, reservations and geometry checks still apply;
the original target state is backed up before installation. Resolve any retained
occupied records through the normal inspected release workflow.

For this installation, `private/cup-dashboard-phone/start-live.command` uses
`devices-combined.json`, the existing ownership file and the canonical Mac paths.
It keeps phone access on HTTP port 8087 and reuses the pairing and Astra settings.
At user startup, `combined_startup.py` can gracefully close the older installation's
dashboard and Mac service only after checking their exact process identities,
ports and idle state. Busy, foreign or unreachable processes abort startup;
the helper never force-kills a process or deletes state. It does not run on import.

## Start the three services

Stop the old terminal/controller before starting a service on the same state.
File-lock checks have been removed for the single-operator setup, including the
Unix-only `fcntl` dependency. Device ownership generations, gateway checks,
reservations and recovery still govern switching and robot commands.

Use the environment containing the working Python camera/model dependencies on
Mac and Pi. From each device's checkout, adjust the following paths. The example
gateway address refers to the same existing Lynx gateway for both devices.

Mac service, accepting only the dashboard on this Mac:

```sh
cd "/path/to/Fail-Safe-Edge-AI-Picker-HT26/capture_and_detect"
python device_service.py --device-id mac --backend python \
  --state-dir "$HOME/.local/share/cup-picker/mac" \
  --model "/path/to/cup-model.pt" \
  --token-file "$HOME/.config/cup-picker/mac-service.token" \
  --robot-id picker-lab-robot \
  --gateway http://192.0.2.20:8000 \
  --listen 127.0.0.1 --port 8091 --live
```

Pi service, reachable from the Mac on the intended network:

```sh
cd "/path/to/Fail-Safe-Edge-AI-Picker-HT26/capture_and_detect"
python device_service.py --device-id pi --backend python \
  --state-dir "$HOME/.local/share/cup-picker/pi" \
  --model "/path/to/cup-model.pt" \
  --token-file "$HOME/.config/cup-picker/pi-service.token" \
  --robot-id picker-lab-robot \
  --gateway http://192.0.2.20:8000 \
  --listen 0.0.0.0 --port 8091 --live
```

Inside a Lynx CLI package that includes service support, `/start.sh --serve`
supplies the native model and worker paths. `--serve` must be the first argument:

```sh
/start.sh --serve \
  --state-dir /state/cli-0.2.0 \
  --token-file /state/private/lynx-service.token \
  --robot-id picker-lab-robot \
  --gateway http://127.0.0.1:8000 \
  --listen 0.0.0.0 --port 8091 --live
```

The Lynx network/mount configuration must expose that service port to the Mac
and make the existing gateway reachable at the supplied address. Loopback works
only when the configured container networking provides it. This command does
not install or restart the gateway.

Lynx package **0.2.8** releases cache for saved camera images and the packaged
native worker/model before measuring startup memory. It also releases image
cache after captures and image downloads. Saved files, placement records and
recovery journals remain intact. The measured **128 MiB** startup requirement
is unchanged; cleanup never treats file sizes as reclaimed memory. This fixes
the observed refusal caused by accumulated camera-image cache when returning
to Lynx. The Mac single-observation scan uses the common `placement_records`
module and does not require the Lynx package on its Python import path.

The 2026-11-16 camera-only rollout checked MacBook, Pi and Lynx scans and repeated
device switches. Final 0.2.8 completed two native scans across separate
activations; 150 one-second samples peaked at 237,088,768 bytes against the
254,283,776-byte shared limit. Memory-limit events did not increase and OOM
counts remained zero. These checks sent no robot motion and do not constitute
physical-picking or long-duration validation.

Lynx package **0.2.9** is installed with the V4 snapshot workflow, smooth handover
paths and no automatic Home after each cup. Its installed source and worker
hashes were verified, and existing device state was preserved. See the
[2026-11-17 workflow update](../lynx/deploy/WORKFLOW_UPDATE_2026-11-17.md) for
validation details. This update was checked without a live scan or robot motion.

All services default to dry-run without `--live`. The dashboard and all configured
services must agree on live/dry-run mode. For a dry-run installation, omit
`--live` from all four commands. Activation loads the selected model; startup
does not scan or move the robot. Service defaults are five raw depth samples per
scan observation. Picks reuse that snapshot without new observations or a result-age limit.
Camera-derived pick height stays off unless explicitly enabled. Existing motion
limits and per-action confirmation remain in force.

## Start the Mac dashboard

```sh
cd "/path/to/Fail-Safe-Edge-AI-Picker-HT26/capture_and_detect"
python web_app.py \
  --device-config "$HOME/.config/cup-picker/devices.json" \
  --output "$HOME/.local/share/cup-picker/dashboard" \
  --live --port 8086
```

Open **http://localhost:8086**. The separate output directory keeps the dashboard's
own process lock apart from a local service's capture directory. With
`--device-config`, omit `--gateway`, `--demo` and inference-only routing flags;
configure model, gateway and perception options on the services themselves.
The HTTP services use the existing explicit commands and confirmation flow.
Astra and saved-cup visual matching require the local Mac configuration above;
they are unavailable on the remote services.

The dashboard opens on **Pick cups**, with the camera, cup selection, preview
and placement grid together. **Robot controls** contains Home, grippers,
controller status and recovery. **Settings** contains speed,
placement-record release and service diagnostics. The device selector remains
above these tabs; **Stop robot** and current-task progress stay in the header
while scrolling. The activity log is under Settings. Changing a
tab only changes the browser view and sends no controller request.

Home and gripper actions appear in the **Robot controls** tab and use the original
gateway's existing commands. **Refresh robot status** reads RAPID execution,
motor state and operation mode. Each movement retains confirmation, ownership,
Stop and recovery checks; actual controller failures are reported. Program
reset/initialization and collision acknowledgement are handled outside this UI.
To pick just one cup, use **Scan workspace**, select it in
the image or cup list, then **Pick selected cup** and review its preview. The
button remains visible with guidance when a fresh scan or selection is needed.
New pick or manual-action previews open their corresponding tab; repeated status
updates do not override a manually chosen tab. Optional command input, saved
references, activity history and camera-check details use expandable sections.

## Switching and interruptions

**Device details** lists the actual reason a target needs inspection, including
reserved placement IDs. A target reservation is reported separately from another
dashboard owner; neither is silently cleared by a switch.

Updated services support the four stage-speed controls under **Settings → Movement
speeds**. Custom profiles transfer with ownership. Older services remain compatible
with the original defaults; switching to one with a custom profile is rejected
before releasing the current device, with instructions to restore defaults or
update that service. No speed is changed automatically.

1. Read **Active device**, **Active model** and **Service status**. Changing the
   menu alone does not activate a device or prove that it supplied a new scan.
2. Finish the current job. Resolve Stop/recovery and outstanding reservations.
   Choose an available device and press **Use device**.
3. The source rejects new work, confirms it is idle, cancels previews and invalidates
   its scan, then durably releases ownership. Only after that acknowledgement can
   the target receive state and activate. The live gateway must also confirm idle,
   unleased robot control. An offline or incompatible target cannot be activated.
4. Wait for activation and model startup, then scan and review the new scene.
   No interrupted pick resumes automatically. Lynx raw images receive cup IDs and
   grid outlines in the browser.

**Connection loss does not trigger automatic fallback.** The dashboard blocks
ordinary actions while ownership is uncertain and reconciles persisted handoff
state on reconnect/restart. It never automatically retries a motion command.
An already dispatched movement may continue; Stop is a best-effort request to the
possible current owner, not a replacement for physical emergency controls.

If model/runtime startup fails, read the service error, correct the cause and
restart that same service using the same persistent state. Retrying **Use device**
does not restart a failed runtime automatically. The dashboard then reconciles
the new service instance; old requests and scan selections stay invalid. Preserve
state and complete any required inspection before continuing.
