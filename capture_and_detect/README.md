# Terminal picker and browser dashboard

There are two independent entry points. Run only one robot controller at a time.

| Entry point | Workflow |
| --- | --- |
| `python capture_and_detect.py` | The V4 terminal capture workflow with the same smooth dual-arm handover as the UI. Enter captures and processes all reachable cups. |
| `python web_app.py --gateway <gateway-url> --live` | The existing browser UI, single-cup selection, Assistant, smooth handover, placement checks, confirmations and recovery. |
| `python web_app.py --demo` | The browser UI with simulated data and no gateway or robot connection. |
| `python picker_workflow.py --commands` | The optional advanced scan/goto/confirm shell sharing the UI backend. |

Commands in this guide run from the `capture_and_detect` directory unless stated
otherwise. From the repository root, prefix the script with `capture_and_detect/`.
For example, `python capture_and_detect/capture_and_detect.py` starts the V4
terminal and `python capture_and_detect/web_app.py --demo` starts the UI preview.

## V4 terminal capture workflow

`capture_and_detect.py` preserves the capture, detection and terminal workflow
from `vision_detection4` at commit
`52f54791e08709ce8e44d1f39b5687085ba8af21`; its `depth_average.py` helper remains
unchanged. Normal dual-arm motion now uses the same smooth `handover_jobs` plan
in `handover_plan.py` as the UI. The terminal does not import the UI backend.
Its capture behavior and command-line options remain:

- Enter captures one scene, detects cups, then picks all reachable cups in
  confidence order, using the same saved positions throughout that cycle.
- Each cup uses the next unoccupied entry in `placing_grid.json`; successful
  placement marks that entry occupied. It stops the cycle at the first failure
  or when there is no free location. There are no UI camera-clearance checks,
  reservations, confirmation previews or persistent recovery journal.
- Normal dual-arm picks use buffered smooth paths, with no automatic Home
  before or between cups. After at least one successful placement, the terminal
  homes once when the batch finishes or the grid is full: left first, then right.
  A failed pick skips this final Home. The terminal uses the shared plan's default
  motion settings; saved dashboard speed settings do not affect it. `--left-only`
  retains its original pick, lift and replace movements and only homes the left
  arm at the end of the batch.
- `q` followed by Enter exits at the prompt. During motion, Ctrl-C inhibits
  subsequent jobs; a submitted movement can finish. The terminal has no
  dashboard Stop/status command interface.
- Supported options are `--gateway`, `--output`, `--conf`, `--left-only`,
  `--camera-z` and `--depth-frames`. There is **no dry-run option** in this
  terminal program. Pressing Enter runs the physical workflow.

The original `main` gateway supports `POST /api/v1/robot/rws/path`; the controller must
have the same `RunPath` routine used by the UI loaded. There is no fallback to
the previous individual-move handover if a path fails.

The launch command is unchanged. Set the existing gateway token, then launch:

```powershell
$env:GATEWAY_TOKEN = "YOUR_ACTUAL_GATEWAY_TOKEN"
python capture_and_detect.py --gateway http://192.168.125.20:8000
```

On macOS/Linux:

```sh
export GATEWAY_TOKEN="YOUR_ACTUAL_GATEWAY_TOKEN"
python capture_and_detect.py --gateway http://192.168.125.20:8000
```

The V4 defaults remain `../Calibration/calibration.json`, `placing_grid.json`
and `Data/Detections`, resolved relative to the script. These defaults are distinct
from the UI's `calibration_grid.json` and seven-cell outside-tray grid. The
terminal has no `--calibration` or `--placing-grid` override.

The source deliberately keeps V4's uppercase `Calibration` path, while the
tracked directory is lowercase `calibration`. Normal case-insensitive macOS and
Windows checkouts resolve it directly. On a case-sensitive Linux filesystem,
create the alias once **from the repository root**, only if it is absent:

```sh
[ -e Calibration ] || ln -s calibration Calibration
```

When using both workflows at different times, give the terminal a separate
capture directory so it does not replace the UI's `cups.json`, `latest.json` or
CSV records:

```sh
python capture_and_detect.py --gateway http://192.168.125.20:8000 \
  --output Data/TerminalDetections
```

This changes only the output location. It does not synchronize the terminal's
placement occupancy with the UI grid, reservations or recovery state. Finish
and close the current controller before switching, and reconcile the physical
scene with the destination layout used by the next controller.

## Browser UI and advanced shell

**All sections below describe `web_app.py`, `picker_workflow.py` and their shared
UI backend unless an entry point is explicitly named. They do not add features
or checks to the V4 terminal capture workflow.**

Scan captures one averaged-depth observation and establishes the cups and
destinations reviewed for single picks or a batch. The **MacBook UI refreshes
source cups and placement-grid occupancy after every successful live placement**, including the
last cup, once both arms have completed all existing movement jobs. It refreshes
the image and source-cup measurements: moved cups use their new positions, missing
cups leave the available list, and new cups become available. Matching cups keep
their IDs; new cups receive new IDs. A confirmed **Pick all** follows this updated
workspace continuously until Stop. When no source cups remain, both arms go
Home once for that empty period, then the camera keeps watching. Newly detected
pickable cups resume automatically when a grid cell is available. If the grid fills during an
active batch, the robot waits while the camera refreshes automatically, then
continues when a cell becomes clear. The
same observation updates destination availability: a removed cup's cell becomes
reusable when the camera confirms it is clear, while newly occupied or uncertain
cells become unavailable. Occupancy changes update the next destinations without
interrupting the completed placement.
**Single-cup and saved/named-cup actions still pick exactly one cup**, then refresh
the workspace for the next request; they do not enter continuous watching or
send the arms Home when the workspace is empty. Completed placements stay
recorded as completed.

This check is enabled for the direct macOS dashboard and the managed `mac`
device. Pi/Lynx selections, direct Windows/Pi dashboards, and both terminal
workflows retain their saved-scan behavior. Dry-run and hover actions do not run
the after-placement check. There is no image-age expiry. Continuous Pick all
does not send the arms Home while any source cups remain, including while waiting
for grid space. Completed cups remain unavailable for another pick.

The UI, Assistant, 60-second confirmations, destination reservations, recovery,
software Stop and smooth handover remain. Receiving offsets are fixed at zero.
The camera and enhanced terminal backend now live in `picker_workflow.py`, with
UI depth sampling in `picker_depth.py`. `web_app.py` is the separate browser
launcher; the V4 script no longer accepts `--web`.

Individual cup sequences send no automatic Home by default. After each handover the left
arm stays above the handover and the right remains 80 mm above placement. In
advanced-shell `--left-only` mode, the left arm stays above the replaced cup.
`--home-after-cup` is an advanced-shell option; manual dashboard Home controls
remain available. These options do not apply to `capture_and_detect.py`.
Continuous MacBook Pick all has a separate Home step only after a camera
observation finds the source workspace empty; it then continues watching.

Windows multi-device setup is described in
[Windows PC / MacBook, Pi and Lynx](DEVICE_DASHBOARD.md#move-the-dashboard-between-mac-and-windows).
The private device configuration is required to show all three processing
choices; `--gateway` alone starts a local-only dashboard. Existing managed UI
launches should keep their current `--device-config` and other arguments with
the `web_app.py` command.

The UI uses the existing YOLO weights and running RAPID program. It does not
install or start RAPID. Dependencies include `ultralytics`, `opencv-python`,
`numpy` and `requests`. `best.pt` is next to the script;
`PICKER_DEVICE=lynx` selects `best_ncnn_model`.

The UI's calibration defaults to `../calibration/calibration_grid.json`, relative
to the script. Keep it paired with the seven-cell outside-tray layout and its
empty-table reference. The separate `calibration/calibration.json` changes the
Z tag offset from 40 to 20 mm and is not a substitute for that validated pair.

`--depth-frames` defaults to **5**. Samples form one observation, not five grid
checks. Cup geometry uses the V4 mask-depth rule and available positive depth
samples, without the removed core/spread rejection. The latest matched RGB and
its raw aligned depth are retained for UI placement checks and saved images.
An ordinary scan can use the latest matching pair and the available samples if
the stream stops advancing. A MacBook after-placement check first waits for a
new frame after motion finishes, then collects one normal depth burst and runs
detection once. It fails without a new first frame. The MacBook check uses that
same observation for **source cups and placement-grid assessment**, including
the table reference, destination depth and occupancy reconciliation. Cups in the
placement area remain excluded from source picking. No second capture or model
run is needed for the grid. A source-only configuration retains the earlier
behavior: cached placement geometry excludes grid cups, while destination
evidence remains from the manual scan and grid occupancy is not reassessed.

Dual-arm travel TCP height is **175 mm**; left-only travel remains **250 mm**.
Requested gripper opening remains **diameter + 70 mm**. `--camera-z` is disabled by default; when enabled it uses
`max(40, round(cup_base_z + 20, 1))` mm TCP for picking and replacing. Otherwise
pick TCP height is 70 mm. No movement occurs merely by launching the UI.

The MacBook dashboard uses GPT-6 Astra, configured in **Settings**;
see [Assistant setup](ASSISTANT.md). The optional `picker_workflow.py --llm`
advanced shell retains its separate Ollama integration, described below.

## Dishwasher tray: offline height preview

`tray_pick_preview.py` calculates proposed heights from an archived scan and
physical measurements. It has no robot connection or execution option. **Tray
picking is not enabled in the web app or terminal by this preview.** Their
existing movement plans still use the original table geometry.

For the tray scan and measurements recorded on 2026-10-30, run from this directory:

```sh
python3 tray_pick_preview.py \
  --inventory Data/Detections/cups_20261030_092031_929056_scan.json \
  --calibration ../calibration/calibration.json --cup-id 2 \
  --cup-top-mm 140 --cup-height-mm 95 --cup-circumference-mm 240 \
  --open-gripper-width-mm 140 --highest-wire-mm 165 --upright-wire-mm 120 \
  --clearance-mm 30
```

The 240 mm measurement is circumference (about 76.4 mm diameter). The 120 mm
measurement is the upright wire tips, not the supporting floor. For a vertical
95 mm cup with its top at 140 mm, the estimated support is 45 mm above the table.
Keeping the original grip point 20 mm below the cup top gives TCP Z=120 mm and
flange Z=279 mm. The explicitly supplied 30 mm lift margin is a planning
assumption, not validated clearance; the calculation considers the cup's lowest
point and does not establish finger or arm clearance.

The proposed grip is level with the upright wire tips. In addition, the saved
camera measurement and manually measured cup-top height differ, some original
placement cells overlap the rack in the image, and the RAPID arm modules use
`MoveAbsJ`, which does not constrain the tool to a vertical path. These need
physical geometry/path validation before enabling tray motion. The preview does
not change calibration, placement reservations, RAPID or live movement targets.

## Run the UI or advanced shell

Launch the local-only UI separately, with `GATEWAY_TOKEN` already set:

```sh
python web_app.py --gateway http://192.168.125.20:8000 --live --port 8087
```

Open `http://localhost:8087`. For the device selector, use the private
`--device-config` command in [device setup](DEVICE_DASHBOARD.md), instead of
`--gateway`. The UI defaults to dry run without `--live`. For a gateway-free
preview use `python web_app.py --demo`.

To use the optional advanced shell:

```sh
python picker_workflow.py --commands --gateway http://192.168.125.20:8000 --dry-run
```

```text
cups> scan
cups> list
cups> show 2
cups> goto 2
... saved-scan plan and planned requests, no robot movement ...
cups> quit
```

`picker_workflow.py` without `--commands` retains the enhanced Enter-to-pick
interface with UI placement reservations, Stop/status and persistent recovery.
It is separate from `capture_and_detect.py` and is not the original V4 program.
The advanced shell runs live unless `--dry-run` is supplied.

If the UI or advanced shell reports an unfinished sequence, inspect both arms
and the cup, recover the robot as needed, then acknowledge recovery through the
UI or the shell's `reset` command. This sends no movement and does not release
reserved placement cells; a fresh scan updates those cells separately. Restarting
or scanning does not clear the robot recovery record.
Use the same UI output directory across restarts; do not delete or replace its
recovery state to bypass it. The V4 terminal capture workflow does not read this
journal.

The advanced shell's `--left-only` option picks, lifts and replaces a selected cup
with the left arm, without a handover or placement-grid update. Add `--dry-run`
to preview that sequence first. Browser mode does not accept `--left-only`.

In `--commands` mode, use `scan` and `goto <ID>` for an available cup. `scan` alone never moves the
robot. The token needs `camera.read`, `robot.control`, and `robot.read` for home
checks. Use one operator instance at a time. Start with both grippers empty and
the known calibrated table/gripper arrangement; `goto` opens the grippers as part
of picking. Check that grid locations marked free are physically clear.

The following table describes **`picker_workflow.py --commands` mode**.
These commands are not available in the original V4 terminal.

| Command | Behavior |
| --- | --- |
| Enter / `scan` / `capture` | Capture and save all candidates; no movement. |
| `start` / `start picking` | Capture a new scan and preview a pick-all batch. Still requires confirmation. |
| `stop` / `halt` / `stop robot` | Immediately inhibit further application commands and request a software RAPID stop for both arms. |
| `status` | Show cached application state, including scan validity and stop/recovery lockout; no robot request. |
| `help` | Show the supported commands. |
| `list` | Show IDs, coordinates, corrected hover positions and blocked reasons. |
| `show 2` | Show the complete saved observation and execution record for cup 2. |
| `goto 2` | Verify, pick, hand over, place, and lift clear; automatic Home is opt-in. |
| `hover 2` | Verify once, move the left arm through ready, hover only. |
| `goto all` / `pick all available cups` | Preview available cups in confidence order, limited by free grid locations. |
| `confirm <code>` | Execute the reviewed action once, within 60 seconds. MacBook UI Pick all follows the changing workspace; other batches keep the reviewed list. |
| `cancel` | Discard a pending proposal; does not stop robot movement. |
| `reset` | Acknowledge manually checking/recovering the robot; no hardware command. Requires a new scan. |
| `q` / `quit` | Exit. If advanced-shell work is active, request software stop before waiting for it to unwind. |

After a successful pick, select another available cup or run `scan` to replace the snapshot. `reset` is for recovery after an interrupted
or unconfirmed action; it is not needed after a successful cycle and is not a
command to move home.

## Start, software stop, status and help

The web dashboard and `picker_workflow.py --commands` shell accept these exact commands without an LLM:

```text
start
status
help
stop
```

`start` means **scan and preview the available cups**, not start RAPID. Review
the numbered cups and destinations, then confirm the proposal. It never resumes
an interrupted sequence or clears recovery. `status` reports the application's
known state; it does not claim the robot is physically stopped or measure a new pose.
Status and Help leave a pending proposal available for review.

The web **Software stop** button and exact `stop`, `halt`, `stop robot`, and
`stop the robot` commands bypass the normal work queue and the LLM. They remain
available while a camera/model request or a pick sequence is running. Stop first
latches a local block on further jobs, then requests `POST /robot/rapid/stop`
through the gateway using the control lease. This stops RAPID in all tasks, so it
affects both arms. A separate request worker allows this call while another job
is waiting for completion. Stop does not open a gripper, home an arm, free a grid
slot, or automatically retry a command.

The display distinguishes **stop requested**, a controller reply confirming
RAPID is **stopped**, and an **unconfirmed** result. Network errors, lease denial,
controller errors or delays can prevent confirmation. A timeout can also occur
after the controller received the stop. Use the robot's physical emergency stop
when immediate stopping is needed; this network control is not an emergency stop.
`cancel` continues to discard only an unconfirmed proposal.

After Stop, proposals and the scan are invalidated, recovery stays required,
and unfinished placement reservations remain occupied. Inspect both arms and the
cup, and recover/restart RAPID through the robot's established operator controls.
An interrupted program can resume its old motion if restarted; this app does not
issue a RAPID start or clear its pending variables. Once active requests have
finished, use the inspection acknowledgement (`reset` in the advanced shell), then scan
again. A new Start request cannot bypass this lockout.

The advanced shell processes normal work on one worker, keeping `stop`,
`status` and `help` responsive. Other actions are rejected while busy; they are
not queued. Ctrl-C, EOF, or quitting during active advanced-shell work requests software
stop before shutdown. An idle exit sends nothing. Closing the browser does not
send Stop. Demo and dry-run Stop make no robot or gateway requests.

## What goto does

The shared plan in `handover_plan.py` adapts the normal smooth handover from
`smooth-dual-arm-handover` at `46fd6f2`, with **no Home inside an individual
cup-transfer plan** by default. It is used by the UI, Assistant actions, shared device
services, `picker_workflow.py` and normal dual-arm picks in `capture_and_detect.py`.
The terminal retains its V4 capture/detection workflow and adds its separate
Home once the batch finishes. Continuous MacBook Pick all has a separate Home
step only after the source workspace becomes empty, then keeps watching.
The branch's simulation-only overlapping multi-cup plan is not used.

Normal paths use **60% acceleration and 40% ramp**. Loaded paths also request
**1.5 m/s²** maximum TCP acceleration/deceleration; turnover keeps **30%
acceleration**. Existing UI speed controls are preserved: default overall speed
is **250 mm/s**, pick/lift and turnover default to **100 mm/s**, and receiving
and placement follow overall speed unless set separately. The slide under the
cup is additionally capped at **150 mm/s**. Every waypoint speed respects the
overall cap. These are the UI's speed settings, rather than the source branch's
50 mm/s final-approach default. Actual speed also depends on the controller.

The UI preserves detected grip yaw/opening, optional camera-Z picking, placement
reservations, Stop/recovery and the existing movement plan. Each cup finishes
completely before the next starts. The MacBook UI then checks the remaining
source cups without sending a robot command; the other runtimes keep using the
saved scan without this additional capture.

1. Open the empty grippers and move the left arm to ready. Set it to the
   detected cup opening there.
2. Follow one path above the cup and straight down to grasp it, using the
   detected yaw. At the same time, move the right hand to its via pose.
3. After both picking and the right via move finish, lift the cup straight up
   and continue to the handover in one path, while the right moves beside it.
4. Wait for both arms, open the right hand, then slide below and straight up to
   the receiving pose in one path, with a 3 mm blend under the cup.
5. Grip with the right hand. Only after it reports completion, release the left.
6. Lift the left hand and lower the right hand simultaneously in straight lines.
   Wait for both to finish before turning the cup.
7. Turn the cup through the sideways midpoint to the downward pose above its
   destination, blending through the midpoint by 10 mm. The left stays clear.
8. Lower the right hand straight to the selected grid location, release and lift
   straight to 80 mm above it. If a camera-clear waiting position is configured, move
   there with the gripper open; otherwise remain above the placement.
9. Complete the UI placement reservation only after every job finishes;
   `capture_and_detect.py` instead marks its chosen grid entry occupied. The next
   cup starts through left ready; neither arm goes home while source cups remain.
   The terminal has a separate final Home after the batch. Continuous MacBook
   Pick all sends both arms Home only when the source workspace becomes empty,
   then keeps watching for new cups.

On the MacBook UI, the workspace and grid refresh runs after step 9 and before any
next cup starts. It waits for both arms, including the move to the camera-clear waiting position.
Grid refresh changes neither the approach, handover and retreat sequence nor the
automatic Home setting.

All normal dual-arm movements use the controller's loaded `RunPath` routine via
`POST /api/v1/robot/rws/path`. Intermediate approach/lift/turn points blend by
10 mm, and each path ends at a fine stop. Gripper actions remain separate.
Each job must return `accepted: true` and exactly `message: "DONE"` before its
dependent jobs start. Both arms share one control lease with at most one request
per arm. A failed path is not retried or replaced with individual moves.
The state reports `handover_plan=smooth_dual_arm_v1` and `home_after_cup=false`
for the default shared workflow. Left-only picking and manual Home are unchanged.

With advanced-shell `--home-after-cup` (or explicit `CupSession(..., home_after_cup=True)`),
step 1 also homes the right arm and checks its joints before picking. After the
right arm clears the handover in step 7, the empty left gripper closes, the left
arm moves laterally to Y=400 mm at Z=690 mm, and homes with a joint check while
the right arm places. After `right_clear`, the right arm homes with a joint check.
All jobs and checks finish before the next cup.

This optional right return does not require a move to Z=600 mm at the placement XY.
That target can be outside the arm's reach despite being inside the configured
workspace box. The upstream branch tried progressively lower heights after a
generic error; this app does not retry an error whose physical outcome is
unknown. When enabled, the right arm homes from its confirmed raised placement position.
This is a changed return path and still needs validation on the physical setup.

The handover barriers preserve both arms' required grip and clearance dependencies.
When automatic Home is enabled, its initial right-arm home verification remains
an additional guard. A failure stops new
dispatch on both branches; already submitted requests are drained, confirmed
peer completions are recorded, and recovery and reservations remain blocked.
No automatic movement retry or resume is performed. Both workflows use the
original gateway API and its ordinary control lease. Lease expiry prevents new
authorized requests but does not stop an already active RAPID movement. The
gateway has no execution-session fencing, lease watchdog or delayed-request
cancellation guarantee. The application's Stop latch prevents further local
dispatch and separately requests the gateway's software Stop.

The detected CUP BASE mask and reliable interior depth provide an estimated
diameter. The requested opening is diameter + 70 mm, converted to a bounded
0–25 mm finger-position command using the calibrated 75 mm closed gap. Missing
diameter falls back to fully open. The nearest assigned handle selects pickup
yaw between 90° and 270°; no usable handle uses 180°. The selected handle, yaw and opening come from the saved snapshot; they are not
recomputed during the pick.

`--left-only` uses the same verified pickup, replaces the cup at its original
position, then lifts clear above it. With `--home-after-cup`, it also closes the
empty gripper, raises to Z=600 mm if needed, moves laterally to Y=400 mm, and homes
the left arm. It sends no right-arm jobs and does not reserve or update a placement location.

After each grip, optional read-only telemetry records the reported finger
position. Live final execution records are also appended to `grips.csv` in the
output directory, with estimated diameter, left/right readings and status.
Unavailable readings stay blank. A later stop can append a status correction;
the inventory JSON remains the authoritative execution record. Dry runs and
hovering do not produce grip rows.

`DONE` confirms a gripper command completed; the API does not prove the fingers
actually hold a cup. This workflow has no independent grasp or placement sensor.

### Fixed receiving pose

The right arm uses the established receiving position `(330, -115, 316)` mm.
Receiving-alignment controls have been removed. Legacy saved offset triples are
accepted during loading but normalized to zero; they cannot alter the motion.

## Placement grid and batches

The advanced shell and browser modes default to the seven-position `placing_grid_outside_tray.json` next to the script;
`--placing-grid /path/to/grid.json` selects another file. Locations are flange
coordinates, used without camera offsets. The next location is the first free
entry in file order that is free and not blocked by an uncertain camera check.

### Camera grid and automatic release

Restart the advanced shell or web app after updating, then use **Scan workspace**
or `scan`. The saved annotated capture shows all 12 actual placement locations,
labelled **P1–P12**, projected onto the table. Cup IDs remain separate. The physical
four-by-three layout appears as three columns and four rows from this camera angle.

Each scan collects **one observation with averaged depth**. Green means
clear table depth, red means a cup or raised object, and gray means unknown depth
or visibility. Scan images show camera evidence; current reservations appear in
the placement grid. The picture is the last scan, not live video.
Remove a placed cup, move your hand clear, then scan again. In live mode, a location
is automatically made available only when the observation confirms clear
table depth and no cup detection overlaps its projected area. A raised depth
cluster blocks release even if YOLO misses the cup. Cups already inside the
placement area are excluded from picking and pick-all batches.

Unknown observations preserve occupancy and set `vision_blocked: true`, preventing
selection even when the previous file status was free. A later clear scan removes
that block. Each fresh live scan updates the current saved grid, including when
its occupancy or reservation records changed during capture. Grid bookkeeping
changes do not interrupt an active handover or trigger extra robot checks. The scan
reconciles saved locations, including reservations left by an interrupted handover:
clear locations become available, while locations containing a cup or object stay
occupied without the old reservation. Old reservation metadata is also removed
from unknown locations, which remain unavailable until a later scan confirms clear
space. **Scan workspace** remains available after a failed movement while the app
is idle, but a software Stop still blocks scanning
until acknowledged. Scanning does not acknowledge robot recovery or enable movement;
the recovery inspection remains required. Dry-run observations are previews only
and do not change the saved grid. MacBook picking uses destination evidence from
the latest scan or successful after-placement grid refresh. After both arms have
finished, that refresh reassesses and reconciles placement locations using the
same camera observation as the source-cup update. Removing a placed cup makes its
cell reusable once the camera confirms clear space; a newly occupied or uncertain
cell is withheld from the next assignment. These occupancy changes do not stop
the batch by themselves. There is no return-phase prefetch or camera assessment
while an arm is still moving. A manual **Scan workspace** also assesses and
reconciles the placement locations as described above.
The checks retain placement geometry, table-reference identity and clear-space
validation. Source-only configurations and runtimes without between-cup checks
keep destination evidence from their manual scan.

It inverts the calibration using all three tag-offset components. Cells are
80 mm square, matching the existing spacing. `--grid-table-z` and
`--grid-cell-size` configure those measurements in both modes;
changing the camera position still requires a new camera calibration.
The depth check requires 99.5% valid coverage of the checked visible area and
table agreement within ±30 mm. Low surface features within that band may pass
the depth check; detected cups still block regardless of height. Connected
raised patches above 30 mm and invalid depth patches block clearance. It checks the
projected volume up to 160 mm above each cell to account for cup-rim parallax.
This checks visible space at scan time; it does not verify grasping or guarantee
the area remains clear after the scan. Raised depth belonging to an unrelated
object beyond a cell's checked volume does not mark that cell occupied. The
calibrated camera rays, including the pixel corners and a 20 mm physical margin,
must confirm that the surface is behind the entire volume. Objects inside it,
foreground objects hiding it, and uncertain depth still prevent clearance.

An optional **frozen empty-table reference** corrects spatial bias in the
below-table check. The app automatically loads the grid's sibling file
`<grid-stem>.table-reference.json` when present. The current outside-tray grid
uses `placing_grid_outside_tray.table-reference.json`, fitted from the independently
clear scan `20261112_115617_554897_scan`. Its provenance includes the saved depth
hash and three advancing frame sequences. No reference is learned during normal
scans or from a scene containing cups. The fitter rejects corrected scans as a
new baseline to prevent cumulative drift.

The reference is a robust plane fitted to observed empty-table samples, balanced
on a 2 mm XY raster, with bounded slope, offset, residuals and spatial support.
Only its negative offset extends the original below-table band. **Both positive
floor mismatch and raised-object checks retain the original 30 mm limit**;
robot calibration, placement coordinates, cup ownership and missing-depth checks
are unchanged. This resolves the saved P9 failure where the measured table was
32–34 mm below ideal Z=0 but within 30 mm of its independently observed reference.
It also clears 18 table-depth unknowns across 44 archived frames; the two frames'
cells with insufficient valid depth remain unknown.

Reference identity binds the calibration, camera intrinsics, grid coordinates,
cell size and nominal table Z, independently of occupancy/reservations. The
reference file is pinned for the session; changes, removal, malformed content
or a context mismatch stop the check rather than silently disabling it. Moving
the camera or changing geometry requires a new verified empty-table reference.
Without a reference file, the original ±30 mm table check remains in effect.

A confidently segmented cup is assigned to the **one cell with the largest
physical overlap**. Related CUP, CUP BASE and CUP HANDLE masks are grouped, and
aligned depth plus calibration maps their pixels into robot X/Y. A unique cell
must contain at least half the usable cup samples, with at least 95% usable
depth and plausible cup height. This also groups duplicate base predictions
inside one body. The owner cell stays occupied even if the cup is low.
The validated segmentation first selects that cell. The connected-depth rule
below must agree with that assignment; it cannot move a cup to another cell or
resolve a tied segmentation assignment.

The dashboard reports confidently located cups and their distinct owner cells
from the saved observation. Two cups in one cell count as two cups but occupy
only that one cell. The count is not a quota: other detected objects, unassigned
cups, raised depth and uncertain visibility still prevent placement. The image
labels distinguish **CUP**, **OBJECT** and **DEPTH CHECK** so an unmatched depth
patch is not presented as an additional cup.

The operator-selected **cup-local uncertainty allowance** also handles depth
fragments that fail the connected-region rules below. Around each confidently
assigned cup, observed raised samples may be excluded from neighbouring cells
when both their perspective-scaled image distance and physical XY distance are
within **40 mm of that same cup**. Small detached fragments can qualify; the
allowance does not depend on depth joining two or more cups. The exact mask must
still supply at least 32 usable samples, at least 95% plausible valid depth and
the same unique physical cell majority used for cup assignment. Samples must
be above 30 mm, no higher than 160 mm and no higher than that cup's observed top
plus 5 mm. This does not change the ordinary 30 mm obstacle threshold.

A wider allowance handles low depth tails outside the grid that overlap a
destination in the camera image: both distances may extend to **80 mm**, but
only for samples **outside every cell's physical footprint**, at most **70 mm
above the table**, and below the same cup-height ceiling. These samples must
connect to the cup's raised exact-mask samples through observed raised depth
inside both distance bounds. A connecting path does not give its in-cell
samples this wider allowance.

These allowances exclude every detection's exact mask, protected mask holes and
missing depth. Known cup cells remain occupied; labelled objects retain their
checks. Excluded samples reduce measured visibility rather than count as clear
table, so at least half the checking area and floor must still be visible and
pass the remaining table/depth checks. A cup assignment records
`depth_uncertainty`, including the two margins and edge/tail pixel counts;
each cell records `cup_uncertain_excluded_pixels`.

**Accepted limitation:** small unlabelled objects close to cups can be missed,
even when disconnected from the cup's depth. Saved-scene tests demonstrate this
for 20 × 20 mm objects at 40 mm and 80 mm heights within the local margin, and
for a low 40 × 40 × 40 mm object outside all cells within the wider tail.
The same small objects with labels remain blocked. Tested 40 × 40 × 80 mm and
60 × 60 × 80 mm obstacles extending beyond the local margin also remain blocked;
this is evidence for those scenes, not a guarantee for all object arrangements.
The saved 08:02:24–08:02:29 checks from 13 November clear both P10 and P11
while preserving the other cups. After a cup was successfully placed in P10,
its foreground depth spill reached 51–65 mm above the table outside all cells;
the earlier 50 mm tail-height cap incorrectly blocked P11. The 70 mm limit
allows those observed tails while retaining the 80 mm distance bound, cup-height
ceiling and connection to the same cup. It does not extend the 40 mm allowance
inside other cells. Fresh multi-frame checks, placement targets,
reservations and recovery handling are unchanged.

In neighbouring cells, the assigned cup's exact mask pixels with valid,
plausible depth are excluded from the obstacle/table tests. The default policy
also groups **connected raised-depth regions** belonging to that cup. A region
for one cup must have at least 32 exact cup pixels covering at least half the
region, and overlap no other detection mask (or box when a mask is unavailable).
Regions touching an image edge or exceeding 131,072 pixels
are rejected. Raised samples must be above 30 mm and at or below 160 mm relative
to the table. Enclosed mask holes and invalid depth stay outside the exemption.

When depth joins **two to four cups in different cells**, the exclusive pixels
of their exact YOLO masks can divide that region. Pixels shared by overlapping
masks cannot seed either cup or connect a part to it; they remain unresolved.
Each other pixel goes to its nearest cup mask in
the image, provided the closest mask wins by more than one pixel of distance.
Exclusive exact-mask pixels stay with their own cup. Each resulting part must connect to
its own mask, contain at least 32 exact pixels, and have at least 50% exact-mask
coverage. Each part must also favour its cup's existing physical cell. Another
detected object anywhere in the original shared region rejects the split.
Cups sharing one cell or masks with insufficient exclusive support retain the
prior checks. All participants must pass the final pooled area checks before an individual
split is applied; failure cancels that split and any connected shared splits.

An unresolved seam between parts normally remains checked. A thin boundary can
instead belong explicitly to **both neighbouring cups** when each observed pixel
is within two image pixels and 12 mm in 3D of both accepted parts. Each boundary
piece must touch both parts; three-way junctions, detached islands, pre-existing
ownership conflicts and pixels already claimed by a fringe remain checked.
All shared boundaries together must cover at most 5% of their original region.
Each cup must still meet the 50% exact-mask coverage rule if assigned the entire
boundary. Its physical owner must also win when competing cells receive every
possible new boundary bin and the owner receives no beneficial new bins. These
checks apply to individual parts and final pooled footprints.

Both cup cells stay occupied. The supported shared boundary is excluded only
when checking other cells, without assigning it an arbitrary single owner. This
clears P10's 25-pixel false block in the saved 11:32:01 verification and the older
28-pixel boundary in the 10:56:38 example. A tall depth patch at the same pixels
still blocks because it fails the 3D continuity check. Records include total
seam pixels and shared-boundary pixels separately. Remaining uncertainty can
still block a cell; the number of blocked cells is not forced to equal cup count.

When that individual split fails, the **cup-edge tolerance** can instead treat a
joined region as belonging collectively to two to four identified cups. Each
cup must still have a confirmed physical owner cell and at least 32 exclusive
raised exact-mask pixels. Together, exclusive cup seeds must cover at least half
the region, and at least half its unique physical XY bins must lie in the union
of their owner cells. Other labelled objects veto the region. Successful
individual splits keep their existing rules.

For samples physically inside another grid cell, the group exemption requires
both image proximity and physical XY proximity within **20 mm of the same cup**.
Only finite, positive-depth exact-mask samples at plausible cup height and owned
by that cup can support the outline. Zero depth, mask holes, overlapping-mask
conflicts and existing foreign ownership cannot expand it. The owning cells
remain occupied; shared samples reduce the visible area of neighbouring cells,
which must still have enough observed clear table. No missing depth is filled.

This is the operator-selected tolerance for joined cup-depth edges. It pools
support across cups without requiring each individual partition's 50% coverage,
the older fringe's 25% area cap, or its 12 mm surface continuity. It can miss
small unlabelled objects touching those edges: the saved P10 scene with an
8 × 20 × 40 mm item joined to a cup by a thin bridge is a documented example.
The same item with an object label still blocks. Samples outside the accepted
cup uncertainty regions retain their checks; this heuristic cannot detect every
obstacle. It clears all six saved 13:24:55–13:25:00 P10 failures; repeated frames
alone could not resolve those persistent depth edges. The rule applies to every
placement cell and does not move the placement targets.

Saved cup assignments include `depth_shared_groups`, listing participating cell
IDs, exclusive support, physical area support, shared pixel counts and retained
foreign-cell pixels. These are group evidence, not a successful individual cup
footprint assignment. Fresh camera checks, motion completion and recovery remain
active. Grid bookkeeping changes do not interrupt the handover. Restart a running
dashboard to load the change.

Observed robot X/Y points occupy a **2 × 2 mm raster**; each occupied bin votes
once, avoiding double counting dense image samples. The exact raised cup-mask
footprint must have one largest-area cell containing at least half that footprint,
including bins outside all grid cells in the denominator. This cell must agree
with the segmentation's owner. Accepted regions for that cup are then pooled:
their unique largest-area cell must agree with the same owner. The full depth
region can extend into gaps or outside the grid without requiring its owner to
contain half that expanded area. For example, P4 keeps its cup's connected depth
when P4 has much more area than P5 but only 47% of the expanded footprint.
All accepted region pixels then belong to that cell, so the same cup's depth
spill does not independently block neighbouring cells. Ties, conflicting cup
identities and rejected regions retain the existing checks. Assignment records
include measured areas, the exact-mask area fraction and the number of depth
pixels attributed to the cup.

This is an approximate observed footprint, not a measurement of hidden space.
**Known limitation accepted for this policy:** an unlabelled object touching a
cup's depth region can be absorbed into the cup and missed in a neighbouring
cell. A separately detected object still blocks. Disconnected depth is checked
unless it qualifies for the separate cup-local uncertainty allowance above.
The saved-frame replay clears P9/P10, while its touching-object counterexample
demonstrates this limitation. Missing depth and insufficient visible table still
block placement; this policy does not force all non-cup cells to become free.

The earlier small-fringe rule remains as a fallback. Aligned depth can
have a coarser outline than RGB, so a small connected depth fringe can belong
to the same cup: it must stay within a 20 mm projected-image margin and a 20 mm
margin around the validated cup XY footprint, within its observed raised-height
range (±5 mm), with at most 12 mm between neighbouring 3D samples. It remains
above 30 mm and below 160 mm. If the extension exceeds 25% of the original mask,
the complete connected outline is checked again with both margins reduced to
10 mm. That narrower outline must meet the same 25% limit; otherwise only the
exact segmentation is used. Only exterior pixels can join this outline; mask holes,
other detections, disconnected patches and abrupt surface changes remain checked
by that fallback. The whole-region rule above can attribute a larger continuous
region without the fallback's 25% area cap or 12 mm surface-step limit.
This does not ignore the whole detection box. At least half of each neighbour's
footprint and checking volume must remain visible.
This treats the cup as occupying its dominant cell; it does not observe hidden
table or prove physical clearance for a cup that overhangs a boundary. A touching,
unlabelled object with an indistinguishable depth surface cannot be separated
reliably by this rule. Missing
masks, low confidence, ambiguous associations or ambiguous ownership retain the
conservative overlap checks. Assignment must yield clear neighbouring cells in
the fresh scan observation; changing/uncertain observations cannot release them.

Restart and scan again to use this policy. New detection records preserve exact
segmentation, including holes; older records without masks retain the conservative
behavior. Reservations remain unchanged until their normal completion or explicit
release. Occupied or unknown locations elsewhere do not block a pick into a
different, verified clear destination.

### Seven-position layout outside the marked tray

`placing_grid_outside_tray.json` now contains **P2–P5 and P8–P10**, with
**90 × 90 mm cells**. P1, P7, P6 and P12 were removed at the operator's request
after a failed move toward P6. P11 was subsequently removed after its raised
placement approach failed; the operator chose seven positions instead of a third
row. IDs were not renumbered. The first row (P2–P5) stays at Y=−260 mm with
X=350, 450, 550 and 650 mm. The second row (P8–P10) stays at Y=−380 mm with
X=350, 450 and 550 mm. Centre spacing between rows is 120 mm. Flange Z stays at
230 mm. In the image they form columns of four and three. The bounding footprint
is 390 × 210 mm, with 10 mm gaps along X and 30 mm along Y.

The selected location's JSON X/Y/Z goes directly to the placing motion; for
example, P3 still places at X=450, Y=−260, flange Z=230 mm, while P9 now places
at X=450, Y=−380, flange Z=230 mm. The second-row shift changes both the camera
overlay and the robot's destinations. The earlier cell-size reduction changed
only the assessment footprints. Removing locations does not prove every
remaining target is reachable.

The matching frozen table reference was derived from the original verified
empty-table capture. Only the last RGB/depth pair of that three-frame scan is
archived, so reassessment for the shifted layout uses that pair, with this
limitation recorded in the reference provenance. The seven-cell reference is
fitted only after all seven unchanged areas independently pass the empty-frame
check without a reference or cup exclusions. The live scene is not used to fit a
new reference; a fresh scan is still required after the layout changes.

The grid file's optional `cell_size_mm` is now authoritative for each capture,
including initial scan placement checks. This file sets it to 90. `--grid-cell-size` is
the fallback for older grid files without that field. Each scan
uses one snapshot and size, and changes invalidate stale placement previews.
The earlier `placing_grid_spaced.json` extends into the marked tray area; use
the outside-tray file for this setup. The layout does not track a moved tray.

Stop the current dashboard while it is idle, then start the outside-tray layout:

```sh
python web_app.py --gateway http://192.168.125.20:8000 \
  --live --port 8086 \
  --placing-grid placing_grid_outside_tray.json --grid-cell-size 90
```

Restart the app and run **Scan workspace** after this update. The seven remaining
occupied/reserved records are preserved. The removed P11 record is retained in
the layout backup; the interrupted run's recovery journal remains unchanged.
Removing a destination does not acknowledge recovery or release a held cup.
A complete backup of the former twelve-position grid is
saved in `output/placing-grid-before-eight-cells-20261111_123234_115061.json`
relative to the repository root. Saved-scan cup assignment, obstacle checks and
reservation rules still apply. Flat tape does not count as
a raised obstacle, and a camera-clear cell is not a reachability guarantee.

`goto 2` and `pick cup 2` select only cup 2. To pick several cups:

```text
cups> scan
cups> goto all
... cups and destination locations, movement speeds, automatic Home setting, confirmation code ...
cups> confirm <the displayed code>
```

`goto all` works without `--llm`. The web app has **Pick all available cups**
with the same confirmation. Only candidates passing pick checks are included,
highest confidence first (ID breaks ties). If there are more eligible cups than
free locations, the preview lists the initial cups and those without a destination.
On the **MacBook UI**, the preview explicitly authorizes following the current
workspace. After each completed placement, the next capture refreshes moved cups,
removes missing cups and adds newly detected pickable cups to the remaining batch.
The next cup uses its fresh measurements and an available destination from the
latest grid assessment, including cells newly cleared by removing a cup. Newly
occupied cells are excluded. Progress shows the actual completed and pending
cup counts as the queue changes. **MacBook Pick all stays active until Stop.**
When the source workspace becomes empty, the dashboard shows **Workspace empty ·
moving both arms Home**, completes Home once for that empty period, then shows
**Watching for cups**. The camera continues refreshing. New pickable source cups
resume the same confirmed run automatically when a grid cell is available.
Both arms stay out of Home while any source cups remain, including cups that
cannot currently be picked or cups waiting for grid space.

When the grid fills during an active batch, the
dashboard shows **Waiting for a clear grid cell** and the number of queued cups.
It hides the cup-step percentage during Home and stationary waiting phases, and refreshes the camera without
sending robot movement. Remove a cup from the grid and move your hand clear;
when a location is confirmed clear, the same confirmed batch resumes automatically.
Stop ends the continuous run and remains available while waiting. A batch cannot start from an already-full
grid: clear a cell and scan before requesting the initial preview.
The initial preview also needs at least one pickable source cup; continuous
watching begins within an already-confirmed run.
A selected or saved/named cup action remains a single pick even when this refresh
finds more cups; it has no continuous watching or empty-workspace Home step.

Other runtimes keep the original reviewed cup list, order and saved coordinates.
Source-only batches retain their existing behavior of finishing when no
destinations remain; automatic waiting requires between-cup grid refresh.
Each cycle ends with both arms at clearance poses by default. When a right-arm
**camera-clear waiting position** is configured, the right arm moves there after
lifting clear. The workspace capture waits for that movement and every left-arm
movement to finish. The review and progress include this waiting-position move only when
configured. Home after every cup remains a separate, explicitly enabled mode;
the continuous run's Home on an empty workspace does not enable that setting.

The live dashboard reads an optional `.right-park.json` sidecar next to
the selected grid, for example `placing_grid_outside_tray.right-park.json`. The
`right_via` preset uses the existing upright right-arm waiting position: the
shared `R_VIA` coordinates and `RIGHT_UP` orientation from the handover plan.
This avoids copying or adjusting those coordinates in the configuration. Use the
exact configured gateway URL in the sidecar:

```json
{
  "schema_version": 1,
  "gateway": "http://192.168.125.20:8000",
  "preset": "right_via"
}
```

Existing explicit position files remain supported as an alternative: omit
`preset`, keep the six-axis `pose` object (`x`, `y`, `z`, `rx`, `ry`, `rz`), and
include `controller_pose` metadata for `arm: "right"`, `tool: "tool0"`,
`wobj: "wobj0"` and `coordinate: "Wobj"`. The configured gateway and applicable
position/frame validation still apply. A missing sidecar keeps the existing
finish above the placement destination. The dashboard loads it on every platform
by default. `--no-right-park` turns it off, except in the Mac workspace-check mode,
which always uses it. Terminal workflows keep their existing movement defaults. `placing_grid_outside_tray.right-park.json` puts the right
arm at x 200, y -380, z 250 (gripper up), behind the grid and out of the camera's
view of it. The grid reaches y -425 mm, past the safe zone's y -400, so the arm
cannot wait to the right of the grid.

Any failure stops the batch before another cup is dispatched. Successful picks
leave any remaining cups available for the next request.

The confirmation binds the initial scan, destinations and motion settings. An
edited grid invalidates the proposal. Before the first live command, the initial
destinations are atomically marked `occupied: true`, `reserved: true`, with a
reservation ID. MacBook Pick all then adjusts unfinished reservations to the
refreshed source queue and current camera-assessed destination availability.
Completed cup history remains recorded; a previously used grid location can be
assigned again after the camera confirms that its cup has been removed. In a
source-only configuration, destination choices stay within the original scan's
clear locations. After each fully confirmed sequence, its location is recorded
occupied and its reservation metadata is removed. A later grid assessment can
update that occupancy. If a scan has already replaced that reservation,
completion preserves the newer scan state and does not interrupt the handover.
This records commanded completion, not vision proof of placement. Dry-run does
not change occupancy.

An interruption leaves unfinished destinations reserved until the next fresh live
scan, including unstarted cups. Scan again when the robot is idle to reconcile
these locations from the camera: empty locations are released and occupied
locations stay unavailable. Uncertain visibility remains blocked. `reset` clears
the robot recovery acknowledgement only; it does not free slots. A scan can update
locations during recovery without clearing the robot's movement lock.
To clear **all** locations after removing the cups, use **Robot controls →
Placement locations → Make all locations available** in the web app, review the
locations and tick the inspection checkbox before confirming. Scan again before
picking. This clears saved occupancy and reservations without clearing a stop or
robot recovery block; the next camera check may mark locations occupied or blocked
again. Normally no manual grid edit is needed after a failed handover: remove any
cup from a location you want to reuse, move your hand clear and scan again.
Do not edit the grid during a sequence. This single-operator version uses no OS
file locks and has no runtime dependency on `fcntl`. Threads within one app still
serialize grid and saved-cup updates; device handoff and recovery checks remain.

## Coordinates and workspace

The saved transform remains `robot XYZ = scale × R × camera XYZ + t`.
The left cup approach, descent and lift add calibration `tag_offset` X/Y once.
The detected base Z is checked. Without `--camera-z`, heights are fixed:

| Position | Gripper TCP Z | Flange Z sent to RWS |
| --- | ---: | ---: |
| Above selected cup, smooth dual-arm | 175 mm | 334 mm |
| Above selected cup, hover / left-only | 250 mm | 409 mm |
| Pick selected cup | 70 mm | 229 mm |

With `--camera-z`, the pick TCP is `max(40, round(cup_base_z + 20, 1))`.
The 159 mm difference is the existing gripper length. `tag_offset[2]` is not
added to either pick-height mode. For example, stored X=600 and offset X=-115 gives
commanded X=485 for approach, pick and lift.

Fixed poses are already flange positions; no camera offset is added to them:

| Pose | Flange X/Y/Z (mm) |
| --- | --- |
| Left ready | 400, 100, 450 |
| Left handover / lifted clear | 350, -100, 655 / 690 |
| Right via | 300, -300, 250 |
| Right beside / below / take | 330, -235, 281 / 330, -115, 281 / 330, -115, 316 |
| Right placement | Selected grid location X, Y, Z |
| Right above placement | Same X/Y, placement Z + 80 |
| Right halfway turn | Midpoint of right-below and right-above-placement |

Left pickup rotation is `(180, 0, detected yaw)`; ready and handover use
`(180, 0, 180)`. Right rotation is `(0, 0, 0)` for receiving
and `(180, 0, 0)` for placement, with `(90, 0, 0)` at the halfway turn.
Home uses the existing RAPID home routines;
the client also checks all seven reported axes within 1 degree of those targets.

Saved XY limits remain X=350–800 and Y=-230–420 mm. The UI no longer applies
the removed whole-plan flange-envelope checks. The gateway retains its own
configured limits. The V4-style per-command height check requires ordinary
movement targets to be at least 199 mm high and no higher than 700 mm; pick and
placement targets may go below 199 mm. The running Pi gateway must
allow Z=700 mm for the updated handover poses; changing the Mac app alone does
not change the Pi's configured limits.
A base qualifies through a CUP body, nearby handle or plausible cup height;
it is not additionally rejected solely because its Z lies outside −30–150 mm.
A cup with Y=-627 mm remains blocked even when
explicitly selected. Place it inside the calibrated work area and scan again;
selecting an ID never bypasses coordinate checks.

These are endpoint checks, not path or collision guarantees. The existing
controller plans joint movement; this client does not turn it into a guaranteed
straight-line path. Grid occupancy is stored state; camera evidence for the chosen
destination is checked before its cup's sequence, not continuously. The workflow
assumes the same cup size and tabletop setup as the existing handover code.

## Snapshot execution, interruption and recovery

The first selected cup is taken from the reviewed scan. No extra camera request
runs when confirming a pick or before its first approach. Stored images have no
movement-age deadline. A successful pick marks that cup completed, so it cannot
be picked again from that scan. An explicit new scan replaces the snapshot.

After each successful live placement, the MacBook UI waits for both arms to
finish all movement jobs, including moving clear of the camera, then captures and refreshes
the source cups and placement grid.
It updates source measurements and the displayed image, including cups that have
moved, appeared or disappeared. Matching cups retain their IDs; new cups receive
new IDs. A confirmed Pick all rebuilds its remaining queue from these current
pickable cups. Single and named picks finish after their one selected cup.
Cups inside the placement area are excluded from source picking. The same
observation assesses placement depth against the table reference and reconciles
grid occupancy. Camera-cleared cells become available; newly occupied or unknown
cells are unavailable for the next pick. Source-only configurations instead keep
the cached placement-area exclusion geometry and the manual scan's destination
evidence, without between-cup grid assessment or reconciliation.

A missing fresh frame, failed detection or invalid observation stops further picks
and requires **Scan workspace**. Moving, adding or removing source cups alone
does not require a manual scan; neither does an ordinary grid occupancy change.
A failed check does not retry the
completed placement or by itself mark that completed motion unconfirmed. The
check also runs after the final cup. Manual scans retain the full placement-grid
assessment and reconciliation. Pi/Lynx, direct non-macOS dashboards and terminal
workflows do not enable this after-placement check.

The destination comes from the latest available clear-cell evidence: the initial
scan for the first pick, then the refreshed grid for subsequent MacBook picks.
Changes to saved occupancy or reservations do not interrupt an active handover;
no extra grid validation is performed between movement steps. Confirmation remains one-use
and expires after 60 seconds. Stop, failed commands, lost control, persistence
failures and uncertain completion
still stop further dispatch and preserve recovery. No movement is retried.

Control leases tolerate different Mac/gateway clocks. For a Lynx gateway without
an HTTP `Date` header, the client measures the initial lease duration using the
gateway's `acquired_at` and `expires_at`, anchored to local monotonic request
time. Request latency counts against that duration. Renewals retain the original
clock reference and cannot grant more than one initial lease duration per request.
An unset Lynx clock (for example, timestamps in 1970) therefore does not falsely
expire a new lease; expired or inconsistent replies still block commands.

`robot_recovery.json` records an active sequence before the first live command.
An interruption requires checking/recovering both arms and the cup, then `reset`
and a fresh scan (`scan` in `picker_workflow.py --commands` mode). Restarting the
program or scanning does not clear this lockout.
Use the same output directory across runs: the journal belongs to that directory.
The flag is cleared only after a fully completed sequence or explicit `reset`.
Dry-run does not acquire control or send any robot HTTP request; it still uses
the camera and model, and does not bypass an outstanding recovery lockout.

## Saved records and future voice control

`Data/Detections` (or `--output`) contains numbered images, RGB, 16-bit depth,
all detections in JSON/CSV, `cups.json` and historical `cups_<capture>.json`.
The selected record keeps its original position, latest verified target and full
planned jobs, completed steps, pending step on failure, and final status (`placed`,
`at_hover`, or `movement_unconfirmed`). `placed` means the robot reported the
sequence complete, not that vision independently confirmed placement.

A future LLM/voice layer can call the same bounded interface:

```python
session.execute_selection(action="goto", capture_id=observed_capture_id, cup_id=2)
```

Use `action="hover"` for approach-only behavior. The language layer should select
an ID from its actual scan; it should not invent coordinates, send raw RWS bodies,
change thresholds, replace outdated capture IDs, or acknowledge recovery itself.

## Local web dashboard

The browser dashboard and advanced shell share `CupSession`, saved-snapshot selection,
fixed motion plan and recovery journal. It shows the latest numbered scan,
selectable cup cards, typed commands, an expiring action confirmation, controller
progress and blocked reasons. It is a local app; no hosting or cloud account is
required. Voice recording is a later addition.

Close any running terminal cup controller before using the web app. Use the same
Python environment where your existing detection script works. From this folder:

```sh
python3 -m pip install -r requirements-web.txt
python web_app.py --demo
```

Open **http://localhost:8080** in your browser. Demo mode uses an illustrated scene,
three synthetic candidates and a small scripted language interpreter. It needs no
gateway token, YOLO weights, Ollama, camera or robot. Click **Scan workspace**, select
cup 2, choose **Pick & transfer**, then **Preview cup 2**. You can also type
`pick cup two`. This is an interface and planning preview, not a physics simulator
or a simulation of all checks during movement.

For real camera detections, use the terminal where `GATEWAY_TOKEN` is already set:

```sh
python web_app.py --gateway http://192.168.125.204:8000 --dry-run
```

This mode captures a scan and previews the plan but sends no robot
commands. As with the advanced-shell dry-run, checks that normally run between physical
steps are not executed. Scan and explicit cup buttons need no language API key.
For flexible requests and real saved-cup matching, configure GPT-6 Astra in
**Settings** and use **Assistant**. The dashboard no longer accepts `--llm`,
`--llm-model` or `--ollama-url`, and makes no Ollama calls or local-model fallback
requests. See [Assistant setup](ASSISTANT.md) for credentials, privacy and API usage.
These Assistant features require the MacBook picker: either the direct dashboard
or the combined `--device-config` dashboard with an in-process `backend: "local"`
Mac entry. Pi/Lynx selections and older separate Mac HTTP services disable Assistant.
Scene-dependent requests take one photo when there is no usable snapshot.
An existing usable snapshot has no age expiry; picking still needs one explicit confirmation.

To enable the physical robot, stop the web server while the robot is idle and
start it with the explicit **`--live`** flag:

```sh
python web_app.py --gateway http://192.168.125.204:8000 --live
```

Omitting `--dry-run` alone does **not** enable movement in the web app. The page
shows **PHYSICAL ROBOT**, and each motion still requires clicking **Confirm cup N**.
The button binds to the exact scan, cup, action and one-use confirmation token;
it expires after 60 seconds. Even typed `goto 2` creates a proposal in the web app.
The advanced shell's exact `goto 2` command executes directly from the selected snapshot. A new scan or request
invalidates an older proposal. Cup IDs belong to one scan and can change on rescan.

Only one action runs at a time. Work happens in a dedicated worker so status stays
responsive. Repeated requests are rejected, never queued or automatically retried.
Refresh reconnects to the existing state and never repeats a movement. Startup
does not restore a scan or proposal; the existing recovery journal still prevents
an unfinished physical sequence from being silently forgotten. The app prevents
another web process from opening the same output folder, but you must still close
other terminal/robot clients yourself.

The app listens on `127.0.0.1` by default. For paired access from a phone on the same
trusted Wi-Fi network, see [Phone access](PHONE_ACCESS.md). Gateway credentials remain in Python; the browser
gets a limited scene summary, the active scan image and saved cup thumbnails. Host/origin and request-token
checks reject requests from unrelated websites. There are no remote assets or
external UI dependencies. Use `--port 8081` if port 8080 is already occupied.

**Cancel request** only discards a proposal. Closing the page, losing its connection,
or stopping this server is not an emergency stop. The UI does not add a gateway or
robot watchdog. Independent robot stopping controls remain necessary. Recovery
acknowledgement requires an explicit inspection checkbox and sends no robot command;
it clears the recovery lock and requires a new scan, but does not clear grid reservations. Do not acknowledge it until the
physical robot and cup have been checked.
An orderly server shutdown asks a continuous batch to finish after its current
completed movement or stationary wait instead of waiting indefinitely for another
cup or free cell. Closing only the browser page does not end the server's run.

The camera image is the **latest scan or successful MacBook workspace check**,
not continuous video. In MacBook grid-refresh mode, placement evidence updates
with each successful after-placement observation. Source-only configurations
retain placement evidence from the manual scan. Progress comes from
the existing execution callbacks and confirmed steps; MacBook Pick all totals
update with its remaining queue, and its final result lists the actual completed
cups and reason for finishing. A completed command sequence
does not independently prove that the cup was gripped or placed. No continuous
collision monitoring, automatic recovery, raw XYZ controls or voice stop is
introduced by this dashboard.

Web implementation: `web_app.py` (browser launcher and local HTTP server),
`web_controller.py` (single controller worker), and `web_static/` (browser interface).
The camera backend is imported from `picker_workflow.py`, using `picker_depth.py`.
The V4 capture workflow in `capture_and_detect.py` and `depth_average.py` is
independent of that backend; normal dual-arm motion shares `handover_plan.py`.
Runtime dependencies are separate from the Pi gateway requirements.

### Robot controls panel

Expand **Robot controls** in the same dashboard. These explicit controls do not
use the language model or a cup detection. They use the original gateway API
from `main`: Home, gripper commands, RAPID/controller status and software Stop.
No modified gateway, execution-session header or direct ABB connection is
required. Home and gripper actions keep their reviewed confirmation and the
existing ownership, Stop and recovery conditions. Actual controller status or
command errors are reported by the operation. Demo/dry-run controls remain
previews without hardware requests.

Program Reset, Initialize and collision acknowledgement are not UI controls.
The UI does not call the removed reset or collision endpoints, read native
collision state, start RAPID or enable motors. Existing `--controller-profile`
settings do not enable direct controller access for these controls.

Implemented operator controls:

- **Movement speeds** sets an overall limit (1–1500 mm/s) and separate pick/lift,
  right-handover travel, cup-turning and placement speeds. Defaults remain overall
  250, pick/lift 100, flip 100, with handover and placement following the overall
  limit. Pick/lift includes the loaded left-arm transfer. Blank handover/placement
  inputs follow the overall limit. Sequence Home remains capped at 200 mm/s
  when explicitly enabled, and turning acceleration remains at 30%. The panel
  shows effective applied speeds. Changes cancel pending previews, apply only to
  future plans and do not change the FlexPendant override. Older remote services
  show only their supported overall-speed control. Managed handoffs transfer custom
  stage settings only to updated services; restore defaults to switch to an older
  service. Existing settings are restored on service restart according to that
  service's saved motion configuration; legacy receiving offsets are reset to zero.
- **Home left / right / Right, then left home** reviews a home request without needing a scan.
  Both runs right first, then left, only after the first arm's completion and
  measured home joints are confirmed. Manual home speed is capped at
  `min(selected speed, 100)` mm/s, separate from homes within the cup sequence.
- **Open / close grippers** operates the selected arm or both, sequentially.
  Opening a gripper can release a held cup. These controls do not reposition it.
- **Refresh robot status** reads RAPID execution, motor state and operation mode
  through `GET /robot/rapid`. It is a timestamped snapshot. Demo and
  dry-run modes show simulated status and send no robot requests.
- **Make all locations available** opens a review for every placement location.
  Remove the cups and confirm that both arms are stopped, then tick the inspection
  checkbox and confirm. It clears occupied, reserved and camera-blocked records,
  preserves all location positions, cancels pending previews and requires a new
  scan. The next camera observation can mark a location occupied or blocked again.
  This action sends no robot command and leaves stop/recovery blocks in place.
  It is available while the app is idle, including after an interrupted sequence;
  an active operation or stop request must finish first. A grid change, disconnect,
  controller restart or new operation cancels the review so it cannot clear a
  different set of records from the one you inspected. It is an explicit panel
  control, not a language-model command.
- **Acknowledge app recovery** leads to the existing inspection acknowledgement. It
  clears only this app's local recovery block and sends no robot command.
- **Software stop** immediately blocks further app commands and requests
  `POST /robot/rapid/stop` using the ordinary gateway lease. The display reports
  whether RAPID confirmed stopping; this network request is not an emergency stop.

Each Home or gripper action has a separate, one-use 60-second preview and inspection
checkbox. Status reads and Stop do not wait for that confirmation. No additional
movement is queued while one is running. Stop invalidates
both cup and robot-control previews; a failed/unconfirmed command requires
inspection again. Manual robot actions invalidate the scan. They never free
reserved placement locations or automatically continue an interrupted pick.

After a collision, inspect both arms, the cup and the return path in person.
If they are trapped or in contact, use the robot's recovery procedure; Home is
not an escape planner. Resolve controller recovery, collision acknowledgement
and motor readiness on the FlexPendant or through the established operator
procedure. Then acknowledge the app block and refresh status. Home and gripper
commands require RAPID running, motors on and AUTO. Once the controller is ready
and the path is clear, review a Home request for the appropriate arm.

`rapid/rapidctl.py` remains a separate terminal administration tool for status,
start, stop, restart and reset. The UI does not launch it or upload RAPID modules.
Its `reset` command clears persistent program values and starts by default;
`reset --no-start` leaves RAPID stopped. These are different from the UI's local
recovery acknowledgement. Run controller administration only after picker work
has finished and the robot's condition has been inspected.

**Deployment:** restart the Mac dashboard after the robot is idle to load these
UI/backend changes. A browser refresh alone does not reload Python code.
The gateway source matches the original `main` implementation. Updating this
checkout does not replace a gateway package already installed on Pi or Lynx.
For a Pi Docker deployment see
[`gateway/README_START.md`](../gateway/README_START.md#deploy-to-the-raspberry-pi).
Do not update/restart either gateway or dashboard during robot movement.

### 3D view

The **3D view** tab shows the real YuMi model (RobotStudio IRB14000_0.5_0.5 and
Smart_Gripper_Servo_Fingers) in the pose the controller reports, plus the cups
from the latest scan and the placement grid. It is read-only: it only sends
GET requests.

- `GET /api/robot-pose` returns the latest readings and never waits for the
  gateway. One shared reader (`robot_telemetry.py`) talks to the gateway, built
  so it cannot crowd out control traffic. The C++ gateway runs every RWS call
  through one lock and allows 64 connections, so requests that arrive too fast
  queue inside it and can starve `/control/renew` ("503 Too many connections").
  The reader:
  - uses one keep-alive connection, with at most one request in flight;
  - leaves the RWS lock free at least half the time;
  - reads the joints of each arm in turn, and a gripper every 2 s;
  - after any error, drops the connection and waits 12 s, then up to 30 s
    (longer than the gateway's 10 s RWS timeout);
  - stops and frees its connection 3 s after the last open 3D tab stops asking.

  It sends only GETs, takes no lease and still runs after a software stop.
  Demo/dry-run shows the home pose marked SIMULATED. Old or missing readings
  are shown as NO LIVE DATA or PARTIAL. The view is never used for any decision.
- The browser plays the timestamped readings back a little behind real time
  (about two reading intervals, 0.2 to 1.2 s) and interpolates between them with
  a monotone cubic (`web_static/3d/pose_interpolation.js`). The motion is smooth
  and never overshoots a reading. Across a gap of more than 1 s it holds the
  last reading and then jumps, rather than drawing motion nobody measured.
- Cups are drawn as plain tapered cups, base up as they stand in the tray.
  They are drawn where the plans grip them: the scanned x/y plus `tag_offset`, published as
  `motion.tag_offset_mm`. A picked cup snaps between the fingers of the gripper
  that holds it, centred on the gripper axis, so no scan or handover offset
  carries over and a placed cup lands on its cell. The view knows the holder from the
  finished step names the dashboard publishes per cup (`motion.completed_steps`):
  - `grip_cup` attaches it to the left gripper;
  - `right_grip` moves it to the right gripper (both arms are still at that
    moment);
  - `right_release`, or `left_release` without a handover, puts it back on the
    table where it was let go.

  Each change happens when the delayed arm playback reaches the time the
  dashboard first saw that step (`motion.done_at`, same clock as the joint
  readings), so the cup leaves the table when the drawn gripper is there. After
  a stop the cup stays with the arm that last held it.
- The joint chain is `rax_1, rax_2, eax_a, rax_3, rax_4, rax_5, rax_6`: the arm
  angle is physically the third joint. The gripper value is each finger's travel
  in mm, the same as `handover_plan` uses it.
- The left gripper has the custom cup fingers (`DownFingerV5.STL`, the same
  part twice, turned 180 degrees about the gripper axis). The build bolts them on
  exactly at the standard finger's screw holes. Like the standard fingers, they
  cross over the centre, with each jaw on the gripper's centre line. Each jaw
  moves out 1 mm per gripper position: 72 mm across the outside of the jaws at
  position 0 and 122 mm at 25 (measured: 75 and 125). The right gripper keeps
  the standard servo fingers.
- The table top is drawn at z = 0 in the base frame, the same as `--grid-table-z`
  (default 0 mm). The lowest 100 mm of the YuMi base sits below it.
- Model assets live in `web_static/3d/`. To rebuild them from new RobotStudio
  exports (each Link body of both arms + Base + gripper, all joints at 0, binary
  STL in mm), run `python capture_and_detect/tools/build_yumi_model.py <stl-dir>
  --left-finger DownFingerV5.STL`
  (`pip install trimesh fast_simplification scipy`). The script finds which arm
  each link belongs to from the geometry and refuses to write if a link is far
  from its joint. `three-bundle.min.js` is three.js r187 (MIT) and can be rebuilt
  with the command in `tools/three-bundle-entry.js`. The CSP forbids inline
  import maps and the Pi has no internet, so it is bundled locally.

## Saved cups

The direct MacBook dashboard uses the configured GPT-6 Astra client for language
and real saved-cup visual matching. It sends reference and candidate photos to
OpenAI when finding and verifying a named cup. YOLO detection and the existing
motion checks remain local. The advanced shell's optional Ollama path is separate and
described under **Local language commands (advanced shell only)** below.

### Save a cup and request it by name

Start the dashboard directly with `python web_app.py --gateway ...` and configure Astra in
**Settings**. Scan, then send `Remember cup 2 as Kasun's cup` in **Assistant** and
click **Save cup reference**. Saving a reference sends no API request or movement.
The **Saved cup references** panel keeps the reference photo across scans and app
restarts. Request `Pick Kasun's cup` to compare the current snapshot's cup crops
with the reference through Astra. Review the saved photo, matched cup and grid
destination inside **Assistant**, then confirm the existing pick/handover/place/clearance
sequence there once. There is no second confirmation in Pick cups. See [Assistant setup](ASSISTANT.md) for the full
workflow. In advanced-shell camera mode, saved-cup matching still requires the legacy
`--llm --llm-model qwen3-vl:4b-instruct` options and a local Ollama server.

These fixed commands remain available in the web command box and advanced shell:

```text
scan
save cup 2 as Kasun mug
saved cups
pick saved Kasun mug
```

`pick Kasun mug` is also accepted when the entire name exactly matches a saved
entry. Names are case insensitive. Saving never moves the robot. A named pick
always needs confirmation, including in the advanced shell (`confirm <code>`).
`forget saved Kasun mug` or **Forget** removes the reference without moving.
Duplicate names are rejected; forget the old entry before saving a replacement.

References live in `Data/Detections/saved_cups.json` under the configured `--output`
directory. Each contains a cropped raw RGB photo and capture metadata, not robot
coordinates. There are at most 32 references, with names up to 48 characters
(letters, digits, spaces, apostrophes, underscores and hyphens; start with a letter).
Straight and curly apostrophes are treated as the same spelling. Use the same
output directory after restart to access the same library. The demo supports named
references using explicit synthetic identities in an isolated temporary library;
it does not perform real visual recognition. Use camera dry-run mode to try real
reference photos and Astra matching.

The model compares up to 12 full-CUP detections, including blocked ones. It
cannot select a different reachable cup when the matching one is blocked. Missing,
ambiguous or malformed responses stop the request without movement. This is
appearance matching, not a guarantee of unique physical identity: indistinguishable
cups and hidden identifying features may require a numbered selection instead.
The VLM returns a compact `match`, `different`, or `unclear` assessment for every
candidate ID. Python derives a unique match only when exactly one candidate matches
and every other candidate is different. Multiple matches or any unclear candidate
remain ambiguous; all different means not found. The explanation cannot override
those assessments, and incomplete or unknown candidate IDs are rejected.
An independent image comparison also vetoes a model-selected cup when another
candidate has a near-identical crop. This catches obvious duplicate views; it is
not a general identity guarantee across different angles or lighting.
Language audit files retain the visual result, candidate IDs and capture ID for
both the proposal and confirmation checks, without duplicating the photos.

Confirmation binds the reference, current scan, candidate ID, destinations and
motion settings. A changed or deleted reference invalidates the proposal. During
confirmation, the VLM checks the selected snapshot candidate against the same
reference. It may reject that target but cannot redirect the pick. It uses the
same saved image, with no camera recapture or image-age expiry. Stop remains
independent of model processing. Numeric picks keep the same robot sequence.

## Local language commands (advanced shell only)

The following Ollama instructions apply only to `picker_workflow.py --llm`.
The original V4 terminal has no language mode. The dashboard uses Astra and does
not start or contact this local model.
Voice recording, speech recognition and spoken replies are not implemented.

```text
Operator sentence -> local Ollama model -> validated action + cup selector
                  -> displayed proposal -> operator confirms exact proposal
                  -> existing CupSession camera checks -> existing robot sequence
```

The default advanced-shell model is `qwen3-vl:4b-instruct`. This vision-language model
accepts text-only requests for command interpretation. Ordinary language commands
send text and scene metadata; saved-cup requests also send the reference photo and
current raw cup crops. Both use a 4096-token context and disable thinking output.
Visual matching sends one labelled comparison image containing the reference and
every candidate. Actual RAM use and performance must be checked on the MacBook.
[Model details](https://ollama.com/library/qwen3-vl:4b-instruct).

In this advanced-shell mode, images are sent only to the configured local Ollama endpoint using its
[vision API](https://docs.ollama.com/capabilities/vision) and
[structured output](https://docs.ollama.com/capabilities/structured-outputs).

Install and open [Ollama for macOS](https://docs.ollama.com/macos), then run:

```sh
ollama pull qwen3-vl:4b-instruct
```

Downloading the app and model needs internet once. This integration sends requests
only to the local Ollama server; the default model then runs offline. It needs no
OpenAI or cloud API key. `GATEWAY_TOKEN` still authenticates the Pi gateway and is
never given to the language model. If Ollama is not running, open its app or run
`ollama serve` in a separate terminal. Do not run two Ollama servers on the same port.

First check the language model alone from `capture_and_detect`:

```sh
python picker_workflow.py --check-llm
```

This sends a synthetic “Hover above cup two” request to local Ollama. It does not
load YOLO, request a gateway token, connect to the camera or contact the robot.
It checks whether the model returns the expected intent for that example. It is
not a complete language accuracy evaluation.

Then try actual camera observations in dry-run:

```sh
python picker_workflow.py --gateway http://192.168.125.204:8000 --llm --dry-run
```

```text
cups> scan
cups> pick cup two
Interpreting locally ...
Preview only: cup 2 — pick with the left arm, hand to the right, place, and finish with both arms at clearance poses.
... Type confirm 12ab34cd within 60s, or cancel.
cups> confirm 12ab34cd
... camera verification and planned robot requests ...
```

Use the actual confirmation code printed in your session. Each code can be used
once; a failed or repeated confirmation never retries motion. After checking the
interpretations, omit `--dry-run` for live movement. Natural-language movements
still require confirmation in live mode because an LLM can misinterpret wording.
Review the cup number against the numbered image and the full action displayed.

| Example sentence | Intended behavior |
| --- | --- |
| `scan the table` | Capture a fresh inventory; no motion. |
| `which cups are available?` | Show the current inventory. |
| `show me cup two` | Show the saved details for cup 2. |
| `pick cup two` / `transfer cup two` | Propose the complete goto sequence for cup 2. |
| `pick all available cups` | Propose a confidence-ordered batch fitting the free grid locations. |
| `hover above cup two` | Propose hover only. |
| `hover above the rightmost cup` | Resolve rightmost in the camera image, then propose or block. |
| `pick the blue cup` / `go to that one` | Ask for an explicit supported reference; no color/gesture data exists. |

Leftmost and rightmost include all detected candidates, even blocked ones. A
blocked edge cup is not replaced by another cup. “Leftmost available,” nearest,
colors, gestures, arbitrary subsets such as “cups 1 and 3,” and automatic multi-action
plans are not supported. Use an explicit cup ID or the separate pick-all action.
The model is prompted to clarify unsupported
requests, but the operator must still inspect every proposal for misinterpretation.

Exact existing advanced-shell commands such as `goto 2` and `hover 2` remain direct
operator commands and do not go through the LLM or proposal step. `scan`, `reset`,
direct movement, quitting, or a new language request invalidates a pending
proposal. `list`, `show 2`, and `help` can inspect it without invalidating it.
The exact word `cancel` discards it. Exact `stop` and its documented aliases
use the independent software-stop path, including while a normal request is busy.
Stop bypasses the model. Natural-language Start can only make a fresh batch preview;
it cannot start RAPID. Keep hardware emergency stopping independent of language.

The text interpreter receives only IDs, image positions, status, and availability.
The separate saved-cup matcher receives reference/candidate photos and temporary IDs.
It cannot send XYZ, arbitrary code, gripper commands, home, reset, speed or
calibration changes. Its JSON output is independently validated; the app resolves
relative references, binds the exact capture, and rejects stale proposals.
Every confirmed movement uses the reviewed snapshot, workspace bounds,
destination reservation, persistent recovery lock and gateway lease. The model's
explanation is labelled separately; success messages come from actual execution.
Schema-valid output can still choose the wrong cup: confirmation is not optional.

The default endpoint is `http://127.0.0.1:11434`. `--ollama-url` accepts loopback
addresses only; redirects and environment proxies are disabled. `--llm-model`
selects another installed local model. Timeout, malformed output or a missing
model cancels the old proposal and never causes fallback motion. The app does not
download models automatically. Offline operation assumes Ollama loads the selected
model locally; the default configuration uses downloaded `qwen3-vl:4b-instruct`.
Known cloud model tags are rejected, but custom server-side aliases are not audited.
Local records under `Data/Detections/language/`
keep each request, interpretation, capture ID, confirmation and result for analysis.

## Next: voice and failure demonstrations

Add push-to-talk recording and local [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
transcription, then pass the transcript to `LanguageController.propose`. It supports
Apple Silicon and offline transcription. Show the transcript, selected cup and
proposed action before an explicit operator confirmation. Recognition uncertainty
or ambiguous requests should request clarification. Keep recovery acknowledgements
and emergency stopping outside the language model.

The web dashboard shows the numbered scan, typed command, proposal, current robot
step and blocked reason. Add spoken feedback from the actual execution result.
Useful dry-run demonstrations include selecting cup 99, selecting
an out-of-workspace cup, letting a proposal expire, and moving a cup between a scan
and confirmation while the robot is stationary. These show the language layer's
limits and the deterministic checks. Do not present this as continuous obstacle
protection or a safety-rated control system.

## Offline tests

For HTTP route coverage, install the optional test dependency too:

```sh
python3 -m pip install -r requirements-web.txt httpx
```

Web tests use a fake camera, fake robot or isolated demo and never contact hardware,
OpenAI or Ollama. HTTP tests skip if FastAPI/httpx are not installed.

From the repository root:

```sh
python3 -m unittest discover -s capture_and_detect -p 'test_*.py' -v
```

Tests use mocked HTTP and synthetic detections. They cover camera pairing,
selection, updated coordinates, handover interlocks, failure/lease behavior,
home verification, persistent recovery, saving and dry-run isolation. They do not
move the physical robot or establish collision clearance for the real setup.

## Mac inference with Pi fallback

See [DEGRADATION.md](DEGRADATION.md) for the optional stateless Mac worker,
preloaded Pi fallback, accepted-provider dashboard and durable event history,
matching-model setup, dry-run disconnection experiment, and original gateway
lease behavior. The coordinator and authoritative robot state
stay on the Pi throughout switching and recovery; Lynx is not needed.

## Windows checkout: table-reference checksum mismatch

The saved empty-table reference includes the exact SHA256 of
`calibration/calibration_grid.json`. Git for Windows can convert LF line endings
to CRLF while checking out a repository. This leaves every calibration number
unchanged but changes the file checksum, producing a table-reference identity
error. The repository now pins calibration and table-reference JSON to LF in
`.gitattributes`, preserving their deployed byte identities in new checkouts.

For an existing Windows checkout, close the dashboard while the robot is stopped.
From `capture_and_detect` in PowerShell:

```powershell
git pull --ff-only
python repair_reference_line_endings.py --apply
python web_app.py --gateway http://192.168.125.20:8000 --live --port 8087
```

Use the existing `GATEWAY_TOKEN` and the gateway's current address. The helper
uses only Python's standard library. Without `--apply`, it only diagnoses the
files. It writes the calibration only if replacing CRLF with LF produces the
**exact** checksum recorded in the saved reference. Other formatting changes,
different calibration values and malformed references are refused. Custom paths
can be supplied with `--calibration` and `--reference`; they must be the same pair
used by the dashboard. Run it with the dashboard closed, then restart and scan.

The helper never rebuilds or modifies the table reference, grid, reservations,
recovery records or camera configuration. A matching calibration does not prove
that the live camera and grid still match: the normal scan checks still apply,
and mismatch errors now identify the affected fields. Do not use an occupied
scene to create a replacement empty-table reference.

This repair addresses direct `--gateway` startup. Managed device switching also
requires byte-identical table-reference files on all devices. An older checkout
may retain a CRLF reference until it is checked out with the new attributes;
the helper deliberately leaves reference bytes alone. Preserve the verified
reference used by the devices rather than regenerating it to resolve formatting.
