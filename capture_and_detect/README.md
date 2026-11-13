# Select a cup and run the handover

**`capture_and_detect.py` is the application entry point.** Run
`python capture_and_detect.py` for the terminal, or
`python capture_and_detect.py --web` for the browser dashboard. Both modes share
the camera, selection, motion-plan and recovery modules. Run only one controller
at a time. For a browser preview without hardware, use
`python capture_and_detect.py --web --demo`.

`scan` detects cups and saves a numbered inventory. **`goto 2` now picks cup 2
with the left arm, hands it to the right arm, places it in the next free grid
location, and finishes with both arms home.** `goto all` offers a separate confirmed
batch. `hover 2` retains the previous
approach-only behavior. IDs refer to the numbered image from the current scan;
they can change after the next scan.

The program uses the existing calibration, YOLO weights and RAPID program over
`/api/v1/robot/rws/move`, `/gripper` and `/home`. It does not install or start RAPID.
Dependencies are `ultralytics`, `opencv-python`, `numpy`, and `requests`.
`best.pt` is next to the script; `PICKER_DEVICE=lynx` selects `best_ncnn_model`.
Calibration defaults to `../calibration/calibration_grid.json`, relative to the
script. This is the verified calibration for the seven outside-tray positions and
their empty-table reference. The latest branch's `calibration/calibration.json`
and `calibration_fused.py` remain available for calibration experiments. The
experimental file changes the Z tag offset from 40 to 20 mm: using it with the
existing grid makes six known-empty cells unknown and invalidates the reference.
Use `--calibration` only with a matching, independently validated grid reference.

## Integration with vision_detection3

The unified entry point retains the browser UI, seven-cell grid, supported gateway
controls, placement uncertainty handling, recovery records and inference failover.
`capture_and_detect_custom.py` remains removed. The incoming branch's model assets,
fused-calibration tool, gateway script addresses and depth-sampling helper remain.

`--depth-frames` defaults to **5** in terminal and hardware web modes; choose 1 to
disable temporal averaging. Each observation collects fresh matched RGB/depth
pairs with strictly increasing sequences, within a single 10-second collection
budget. The latest RGB and its **raw aligned depth** are retained for grid checks
and saved images. Cup coordinates average the per-frame interior depths, with
per-frame camera/robot Z recorded in detection JSON and robot Z in CSV and cup
inventory. An invalid base core or depth spread above 20 mm leaves the cup without
a usable pick coordinate. This preserves the existing centre-depth guard.

The five samples form one observation, not five clear-grid checks. Three
consecutive independent clear observations are still required for the batch's
post-home placement check; averaging never fills a depth hole or replaces that
raw-frame evidence. The observation age starts before the oldest sample. A
frozen/restarted stream or incomplete sample set is rejected. Only the latest raw
image pair is archived; per-frame depths are recorded as object measurements.
A locked detections CSV falls back to a capture-specific CSV so an open spreadsheet
does not interrupt capture storage.

The user-approved incoming movement settings are **250 mm travel TCP height** and
**diameter + 70 mm gripper opening**. `--camera-z` is available in terminal and web
modes and remains **disabled by default**. When enabled it uses
`max(40, round(cup_base_z + 20, 1))` mm TCP for picking, with the same height used
when replacing a cup in left-only mode. The guarded planner, confirmation and
fresh-observation checks still apply. Without this flag, pick TCP stays at 70 mm.

The incoming experiment that left arms away from home is not enabled: the working
UI/grid workflow needs both arms home to obtain unobstructed placement evidence.
The established overlapping handover and return sequence is preserved, including
the corrected right-arm return and completion checks. No automatic movement is
performed merely by launching the app.

Natural-language input is available with `--llm`; see **Local language commands**
below. It uses an offline Ollama model on the Mac and the same cup-selection checks.

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

The detector requires valid depth near each CUP BASE centre and
across its interior. It uses the interior median instead of edge/background
samples; an unreliable base stays visible but has no usable robot coordinates.
The saved detection and inventory include a `depth_quality` explanation. Restart
the app and scan again to apply this check; archived scans are not rewritten.

## Run

From the `capture_and_detect` directory on the Mac, preview the whole sequence:

```sh
export GATEWAY_TOKEN="BAD_TOKEN_OPERATOR"
python capture_and_detect.py --gateway http://192.168.125.204:8000 --dry-run
```

Use the configured operator token if yours differs. The Pi address above is the
current lab address; update it if the Pi's address changes. The default address
without `--gateway` remains `http://169.254.125.202:8000`.

```text
cups> scan
cups> list
cups> show 2
cups> goto 2
... verification image and all planned requests, no robot movement ...
cups> quit
```

For live operation, restart without `--dry-run`:

```sh
python capture_and_detect.py --gateway http://192.168.125.204:8000
```

The terminal-only `--left-only` option picks, lifts and replaces a selected cup
with the left arm, without a handover or placement-grid update. Add `--dry-run`
to preview that sequence first. Browser mode does not accept `--left-only`.

Then `scan` and `goto <ID>` for an available cup. `scan` alone never moves the
robot. The token needs `camera.read`, `robot.control`, and `robot.read` for home
checks. Use one operator instance at a time. Start with both grippers empty and
the known calibrated table/gripper arrangement; `goto` opens the grippers as part
of picking. Check that grid locations marked free are physically clear.

| Command | Behavior |
| --- | --- |
| Enter / `scan` / `capture` | Capture and save all candidates; no movement. |
| `start` / `start picking` | Capture a new scan and preview a pick-all batch. Still requires confirmation. |
| `stop` / `halt` / `stop robot` | Immediately inhibit further application commands and request a software RAPID stop for both arms. |
| `status` | Show cached application state, including scan validity and stop/recovery lockout; no robot request. |
| `help` | Show the supported commands. |
| `list` | Show IDs, coordinates, corrected hover positions and blocked reasons. |
| `show 2` | Show the complete saved observation and execution record for cup 2. |
| `goto 2` | Verify, pick, hand over, place, and home both arms. |
| `hover 2` | Verify once, move the left arm through ready, hover only. |
| `goto all` / `pick all available cups` | Preview available cups in confidence order, limited by free grid locations. |
| `confirm <code>` | Execute the exact batch or language proposal, once, within 60 seconds. |
| `cancel` | Discard a pending proposal; does not stop robot movement. |
| `reset` | Acknowledge manually checking/recovering the robot; no hardware command. Requires a new scan. |
| `q` / `quit` | Exit. If terminal work is active, request software stop before waiting for it to unwind. |

After a live action, run `scan` again. `reset` is for recovery after an interrupted
or unconfirmed action; it is not needed after a successful cycle and is not a
command to move home.

## Start, software stop, status and help

The web dashboard and terminal accept these exact commands without an LLM:

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
finished, use the inspection acknowledgement (`reset` in the terminal), then scan
again. A new Start request cannot bypass this lockout.

The interactive terminal processes normal work on one worker, keeping `stop`,
`status` and `help` responsive. Other actions are rejected while busy; they are
not queued. Ctrl-C, EOF, or quitting during active terminal work requests software
stop before shutdown. An idle exit sends nothing. Closing the browser does not
send Stop. Demo and dry-run Stop make no robot or gateway requests.

## What goto does

The shared plan in `handover_plan.py` defines the cup handover poses and sequence.
Without a speed override, normal moves use
**250 mm/s** and homes within the cup sequence use **200 mm/s**, with
**100% acceleration and ramp**. Left-arm descent, loaded lift and travel to the
handover use at most **100 mm/s**. The two cup-turning moves use **100 mm/s**
and **30% acceleration**, with 100% ramp.
Every job must return `accepted: true` and exactly `message: "DONE"` before the
next job on that arm starts. Explicit handover dependencies allow the two arms
to overlap only after the required clearance/grip steps complete. An explicit
`--speed` value sets normal speed (1–1500 mm/s) and caps
sequence home speed at the lower of that value and 200 mm/s; loaded picking
and turning use the lower of that setting and 100 mm/s. **Apply speed** in the web panel applies the
same caps. For example, applying 75 mm/s sets normal, loaded-pick, turning and
sequence-home speeds to 75 mm/s. Actual speed
also depends on the robot controller. Both arms share one control lease and
renewal loop, with at most one outstanding request per arm, an initial right-arm
home check and a camera verification before each cup's motion.

1. Verify the selected cup, grip geometry and destination with three consecutive
   clear camera observations, then close the empty right gripper, home the right arm and check
   its measured joint positions.
2. Close the empty left gripper and move the left arm to ready.
3. Approach above the verified cup with its detected grip rotation, open the
   left gripper to the measured opening, descend, grip, and lift.
4. After the left arm lifts the cup, move it toward the handover while the closed
   right hand moves through the existing approach positions to beside the handover.
5. Wait for both arms to reach those positions, then open the right hand, move
   below and up to receive the cup and grip.
6. Only after the right grip reports completion, release the left hand and lift it clear.
7. Lower the right arm, turn sideways at the midpoint between the lowered
   receiving pose and the raised destination, then turn downward above that
   destination. Once it clears the handover, close the empty left gripper, move
   laterally to Y=400 mm at its lifted Z=690 mm, then home it and check its
   joint positions. This left-arm return runs concurrently with step 8.
8. Lower the right hand to the selected grid location, release, lift to the
   confirmed clearance 80 mm above placement, close the empty gripper, then
   request the controller's right Home and check its joint positions.
9. Wait for both branches to finish before completing the placement reservation
   or beginning another cup.

The right return no longer requires a move to Z=600 mm at the placement XY.
That target can be outside the arm's reach despite being inside the configured
workspace box. The upstream branch tried progressively lower heights after a
generic error; this app does not retry an error whose physical outcome is
unknown. The right arm now homes from its confirmed raised placement position.
This is a changed return path and still needs validation on the physical setup.

The two overlaps match the original branch's handover barriers. The initial
right-arm home verification remains an additional guard. A failure stops new
dispatch on both branches; already submitted requests are drained, confirmed
peer completions are recorded, and recovery and reservations remain blocked.
No automatic movement retry or resume is performed. The Python gateway supports
independent arm requests. Lynx's deployed C++ gateway concurrency and late-request
cancellation behavior have not been validated; its missing session fencing means
the newer Python gateway's cancellation guarantees cannot be assumed.

The detected CUP BASE mask and reliable interior depth provide an estimated
diameter. The requested opening is diameter + 70 mm, converted to a bounded
0–25 mm finger-position command using the calibrated 75 mm closed gap. Missing
diameter falls back to fully open. A nearby unambiguous handle selects pickup
yaw between 90° and 270°; no usable handle uses 180°. An ambiguous handle blocks
picking. Fresh verification must keep yaw within 5° and finger position within
2.5 mm of the scanned plan, or a new scan and review are required.

`--left-only` uses the same verified pickup, replaces the cup at its original
position, closes the empty gripper, raises to Z=600 mm if needed, moves laterally
to Y=400 mm, and homes the left arm. It sends no right-arm jobs and does not
reserve or update a placement location.

After each grip, optional read-only telemetry records the reported finger
position. Live final execution records are also appended to `grips.csv` in the
output directory, with estimated diameter, left/right readings and status.
Unavailable readings stay blank. A later stop can append a status correction;
the inventory JSON remains the authoritative execution record. Dry runs and
hovering do not produce grip rows.

`DONE` confirms a gripper command completed; the API does not prove the fingers
actually hold a cup. This workflow has no independent grasp or placement sensor.

### Adjusting receiving-gripper alignment from the web app

Open **Robot controls → Handover alignment** to correct the right hand's
receiving position without updating the Pi gateway or RAPID. X/Y/Z offsets are
in robot-base millimetres, relative to the existing flange position
`(330, -115, 316)`. They are not camera/image directions. The startup offset is
`(0, 0, 0)`; the application does not infer a correction from a phone video.

After the current collision has been inspected and recovered, establish the
robot-axis direction and the centring correction at the real setup. For a
horizontal centring problem, leave Z at zero unless a height correction has
also been taught. Apply the small measured X/Y correction, scan again, select
one cup and review the displayed receiving XYZ before confirming the trial.
For example, an **illustrative** X offset of `+2` changes receiving X from 330
to 332 mm; that example is not a diagnosed direction for this mug.

**Apply alignment** changes future plans only and sends no movement. It cancels
cup and operator previews and invalidates the scan. **Reset alignment** returns
the offsets to zero, also without movement. Settings last for this app session
and reset on restart. The UI accepts 0.5 mm increments within ±20 mm per axis;
those bounds limit configuration errors, not collision clearance.

The take, below, beside and post-release lower points move together. The turning
midpoint is recomputed; left-arm targets, pick coordinates, orientations and
placement destinations stay as before. With a nonzero offset, the four nearby
right-arm moves use at most 25 mm/s (or the selected lower speed), 20%
acceleration and 10% ramp. This affects arm motion, not finger closing speed or
force, and the controller still executes joint-space `MoveAbsJ` moves.

The cup confirmation and saved language/execution records include the exact
offset and effective receiving pose. A settings change invalidates an older
confirmation, including changes before dispatch acquires the session lock.
The right grip must still report DONE before left release; a failed grip never
causes early release, automatic retry or a return-home command. Even contact
can reduce sideways pushing but does not guarantee a collision-free grip.

Cup-load handling is defined in `rapid/RightArmInstructions.mod`.
That behavior runs on the robot controller when its updated RAPID module is loaded;
changing the Python files alone does not install it. The app uses the same
right-gripper grip/open requests. This integration does not deploy or modify RAPID.

## Placement grid and batches

Terminal and browser modes default to the seven-position `placing_grid_outside_tray.json` next to the script;
`--placing-grid /path/to/grid.json` selects another file. Locations are flange
coordinates, used without camera offsets. The next location is the first free
entry in file order that is free and not blocked by an uncertain camera check.

### Camera grid and automatic release

Restart the terminal or web app after updating, then use **Scan workspace**
or `scan`. The saved annotated capture shows all 12 actual placement locations,
labelled **P1–P12**, projected onto the table. Cup IDs remain separate. The physical
four-by-three layout appears as three columns and four rows from this camera angle.

Each scan collects **three distinct, advancing RGB/depth frames**. Green means
clear table depth, red means a cup or raised object, gray means unknown depth or
visibility, and amber means reserved. The picture is the last scan, not live video.
Remove a placed cup, move your hand clear, then scan again. In live mode, a location
is automatically made available only when all three observations confirm clear
table depth and no cup detection overlaps its projected area. A raised depth
cluster blocks release even if YOLO misses the cup. Cups already inside the
placement area are excluded from picking and pick-all batches.

Unknown observations preserve occupancy and set `vision_blocked: true`, preventing
selection even when the previous file status was free. A later clear scan removes
that block. Every update checks that the grid has not changed during capture and
uses the same atomic file lock as reservations. Camera checks never clear reserved
locations or robot recovery. During recovery, and in dry-run mode, observations
are previews only and do not change the saved grid. Motion verification captures
also draw the overlay but do not release locations.
The terminal and hardware dashboard require **three consecutive clear destination
observations**, with at most six foreground attempts per cup. A depth-only anomaly
or an occluded frame resets the clear count; it is not averaged away or treated
as a vote for clearance. In these foreground checks, a detected cup or other
labelled object at the destination, malformed evidence, changed calibration,
geometry or reference, stale frames, changed target/grip geometry or Stop blocks
the attempt. Persistent depth uncertainty still blocks after the bounded attempts.
Saved verification records retain every check and the reason it counted or failed.
Each assessed cell also records `raised_depth_pixels`,
`raised_depth_inside_cell_pixels` and `raised_depth_outside_cell_pixels`. These
distinguish physical intrusion from foreground depth overlapping the cell's
camera projection. Outside-cell depth can still hide the destination, so these
diagnostics do not relax clearance checks or change placement decisions.

In a live batch, after `right_clear` has reported completion and been recorded,
one camera worker can collect up to two observations for the next cup while the
arms finish returning home. It does not move the robot or update reservations.
The next cup waits for all current jobs and home checks to succeed, then takes
at least one new observation after that barrier. The right arm may still hide
cups or the destination during its return. These early observations are optional:
a structurally valid but nonclear view, missing/ambiguous target, changed grip
estimate or stale early image discards all early credit. A later clear background
image cannot restore it. The app then requires three consecutive clear images
after home, within six foreground attempts; the two early images do not use that
budget. This does not classify the obstruction as an arm or mark hidden space
clear. Malformed evidence, changed source/calibration/geometry/reference,
repeated camera identities and capture errors still fail closed.
All accepted evidence must remain fresh through the pick's freshness gates.
Stop or a failed return discards the prefetch and drains its worker before
another operation can use the camera.
The first cup has no preceding return phase, so its checks run before motion.
There is still no second camera check at left ready or a retry of robot movement.
These checks do not observe the entire transfer continuously.

Library callers retain the legacy single-frame path unless they explicitly set
`CupSession(..., verification_frames=3)`. Its existing stationary three-clear
recheck for eligible unknown depth remains available; production launchers use
the three-frame mode described above.

The overlay uses placement **X/Y and table Z=0 mm**, not flange Z=230 mm.
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
from the **last frame**, without adding repeated detections across the three
frames. Two cups in one cell count as two cups but occupy only that one cell.
If per-cell counts change during the scan, the dashboard says so and keeps all
observed blocks. The count is not a quota: other detected objects, unassigned
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
footprint assignment. Fresh camera checks, motion completion, recovery and
reservation checks remain active. Restart a running dashboard to load the change.

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
all three fresh scan frames; changing/uncertain observations cannot release them.

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
including motion verification. This file sets it to 90. `--grid-cell-size` is
the fallback for older grid files without that field. Each three-frame scan
uses one snapshot and size, and changes invalidate stale placement previews.
The earlier `placing_grid_spaced.json` extends into the marked tray area; use
the outside-tray file for this setup. The layout does not track a moved tray.

Stop the current dashboard while it is idle, then start the outside-tray layout:

```sh
python capture_and_detect.py --web --gateway http://192.168.125.20:8000 \
  --live --llm --llm-model qwen3-vl:4b-instruct --port 8086 \
  --placing-grid placing_grid_outside_tray.json --grid-cell-size 90
```

Restart the app and run **Scan workspace** after this update. The seven remaining
occupied/reserved records are preserved. The removed P11 record is retained in
the layout backup; the interrupted run's recovery journal remains unchanged.
Removing a destination does not acknowledge recovery or release a held cup.
A complete backup of the former twelve-position grid is
saved in `output/placing-grid-before-eight-cells-20261111_123234_115061.json`
relative to the repository root. The usual three-frame checks, cup assignment,
obstacle checks and reservation rules still apply. Flat tape does not count as
a raised obstacle, and a camera-clear cell is not a reachability guarantee.

`goto 2` and `pick cup 2` select only cup 2. To pick several cups:

```text
cups> scan
cups> goto all
... cups and destination locations, normal/turning/home speed, confirmation code ...
cups> confirm <the displayed code>
```

`goto all` works without `--llm`. The web app has **Pick all available cups**
with the same confirmation. Only candidates passing pick checks are included,
highest confidence first (ID breaks ties). If there are more eligible cups than
free locations, the preview lists the included cups and those left out. No new
cup is silently substituted. Each included cup must match one fresh observation
before its own sequence. Each cycle ends with both arms home.
Any failure stops the batch before another cup is dispatched. Scan again afterward.

The confirmation binds the scan, cup order and exact destinations. An edited grid
invalidates the proposal. Before the first live command, all destinations in the
batch are atomically marked `occupied: true`, `reserved: true`, with a reservation
ID. After each fully confirmed sequence, its location stays occupied and its
reservation metadata is removed. This records commanded completion, not vision
proof of placement. Dry-run does not change occupancy.

An interruption leaves unfinished destinations reserved, including unstarted cups.
`reset` clears the robot recovery acknowledgement only; it does not free slots.
To clear **all** locations after removing the cups, use **Robot controls →
Placement locations → Make all locations available** in the web app, review the
locations and tick the inspection checkbox before confirming. Scan again before
picking. This clears saved occupancy and reservations without clearing a stop or
robot recovery block; the next camera check may mark locations occupied or blocked
again. To recover just one **reserved** slot manually, stop controllers while the robot is idle, inspect the arms and
placement area, remove any cup from that slot, then set its `occupied` to `false`
and remove its `reserved` and `reservation_id` fields. Preserve its ID and coordinates.
Restart and scan afterward; the camera must confirm its visibility before selection.
Do not edit the grid during a sequence. Run only one robot controller at a time;
file locking does not coordinate independent robot clients.

## Coordinates and workspace

The saved transform remains `robot XYZ = scale × R × camera XYZ + t`.
The left cup approach, descent and lift add calibration `tag_offset` X/Y once.
The detected base Z is checked. Without `--camera-z`, heights are fixed:

| Position | Gripper TCP Z | Flange Z sent to RWS |
| --- | ---: | ---: |
| Above selected cup | 250 mm | 409 mm |
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

Saved XY limits remain X=350–800 and Y=-230–420 mm. Corrected flange targets
must also fit the configured gateway limits: X=180–800, Y=-400–500, Z=50–700 mm.
Ordinary movement targets must be at least 199 mm high; the pick and placement
targets may go lower within the flange limits. The running Pi gateway must
allow Z=700 mm for the updated handover poses; changing the Mac app alone does
not change the Pi's configured limits.
Full picking additionally requires a detected cup-base Z of −30–150 mm.
A candidate permitted only for hovering is labelled `HOVER ONLY` in the image
and `hover only` in the list. A cup with Y=-627 mm remains blocked even when
explicitly selected. Place it inside the calibrated work area and scan again;
selecting an ID never bypasses coordinate checks.

These are endpoint checks, not path or collision guarantees. The existing
controller plans joint movement; this client does not turn it into a guaranteed
straight-line path. Grid occupancy is stored state; camera evidence for the chosen
destination is checked before its cup's sequence, not continuously. The workflow
assumes the same cup size and tabletop setup as the existing handover code.

## Verification, interruption and recovery

The selected cup must match a fresh observation uniquely within **20 mm in 3D
and 40 pixels**, with unchanged calibration and advancing camera sequences.
This verification window completes before any robot command in that cup's sequence.
Its final accepted coordinates are used for the pick; there is no second camera check at left
ready. Other cups' IDs in verification images never change the user's selection.

Image age is checked before starting, approaching, descending and gripping.
The default deadline is **15 seconds**;
`--max-result-age` changes it. Capture, inference, saving and movement time all
count toward that deadline. If homing, the ready move or the approach exceeds it,
the next approach, descent or grip is blocked without taking another image.
Changes to the scene after the initial verification are not reobserved during
that cup's sequence. After gripping, the fixed handover continues without requiring
the cup to remain at its original camera position. This is not continuous scene monitoring.

Camera errors, invalid/missing/ambiguous targets, failed persistence, failed
commands or a lost control lease stop further dispatch. No automatic retry,
release, return-home or reverse sequence is attempted after failure. An already
sent command may still finish. `reset`, terminal exit/interrupt, and lease expiry
are not hardware emergency stops; only an explicit Stop or an active terminal
exit/interrupt additionally requests the software stop described above.

Control leases tolerate different Mac/gateway clocks. For a Lynx gateway without
an HTTP `Date` header, the client measures the initial lease duration using the
gateway's `acquired_at` and `expires_at`, anchored to local monotonic request
time. Request latency counts against that duration. Renewals retain the original
clock reference and cannot grant more than one initial lease duration per request.
An unset Lynx clock (for example, timestamps in 1970) therefore does not falsely
expire a new lease; expired or inconsistent replies still block commands.

`robot_recovery.json` records an active sequence before the first live command.
An interruption requires checking/recovering both arms and the cup, then `reset`
and `scan`. Restarting the program or scanning does not clear this lockout.
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

The browser dashboard reuses the terminal's `CupSession`, camera verification,
fixed motion plan and recovery journal. It shows the latest numbered scan,
selectable cup cards, typed commands, an expiring action confirmation, controller
progress and blocked reasons. It is a local app; no hosting or cloud account is
required. Voice recording is a later addition.

Close any running terminal cup controller before using the web app. Use the same
Python environment where your existing detection script works. From this folder:

```sh
python3 -m pip install -r requirements-web.txt
python capture_and_detect.py --web --demo
```

Open **http://localhost:8080** in your browser. Demo mode uses an illustrated scene,
three synthetic candidates and a small scripted language interpreter. It needs no
gateway token, YOLO weights, Ollama, camera or robot. Click **Scan workspace**, select
cup 2, choose **Pick & transfer**, then **Preview cup 2**. You can also type
`pick cup two`. This is an interface and planning preview, not a physics simulator
or a simulation of all checks during movement.

For real camera detections and your local Ollama model, use the terminal where
`GATEWAY_TOKEN` is already set and keep Ollama running:

```sh
python capture_and_detect.py --web --gateway http://192.168.125.204:8000 --llm --dry-run
```

This mode performs camera verification and previews the plan but sends no robot
commands. As with the terminal dry-run, checks that normally run between physical
steps are not executed. Without `--llm`, Scan and the explicit cup buttons still
work; typed natural language requires `--llm`.

To enable the physical robot, stop the web server while the robot is idle and
start it with the explicit **`--live`** flag:

```sh
python capture_and_detect.py --web --gateway http://192.168.125.204:8000 --llm --live
```

Omitting `--dry-run` alone does **not** enable movement in the web app. The page
shows **PHYSICAL ROBOT**, and each motion still requires clicking **Confirm cup N**.
The button binds to the exact scan, cup, action and one-use confirmation token;
it expires after 60 seconds. Even typed `goto 2` creates a proposal in the web app.
The terminal's exact `goto 2` command executes directly after verification. A new scan or request
invalidates an older proposal. Cup IDs belong to one scan and can change on rescan.

Only one action runs at a time. Work happens in a dedicated worker so status stays
responsive. Repeated requests are rejected, never queued or automatically retried.
Refresh reconnects to the existing state and never repeats a movement. Startup
does not restore a scan or proposal; the existing recovery journal still prevents
an unfinished physical sequence from being silently forgotten. The app prevents
another web process from opening the same output folder, but you must still close
other terminal/robot clients yourself.

The app listens on `127.0.0.1` only. Gateway credentials remain in Python; the browser
gets a limited scene summary, the active scan image and saved cup thumbnails. Host/origin and request-token
checks reject requests from unrelated websites. There are no remote assets or
external UI dependencies. Use `--port 8081` if port 8080 is already occupied.

**Cancel request** only discards a proposal. Closing the page, losing its connection,
or stopping this server is not an emergency stop. The UI does not add a gateway or
robot watchdog. Independent robot stopping controls remain necessary. Recovery
acknowledgement requires an explicit inspection checkbox and sends no robot command;
it clears the recovery lock and requires a new scan, but does not clear grid reservations. Do not acknowledge it until the
physical robot and cup have been checked.

The camera image is the **latest scan**, not continuous video. Progress comes from
the existing execution callbacks and confirmed steps. A completed command sequence
does not independently prove that the cup was gripped or placed. No continuous
collision monitoring, automatic home/recovery, raw XYZ controls or voice stop is
introduced by this dashboard.

Web implementation: `web_app.py` (local HTTP and compatibility launcher), `web_controller.py`
(single controller worker), and `web_static/` (self-contained browser interface).
The camera backend is imported from `capture_and_detect.py`. Existing
`python web_app.py ...` commands remain supported; the documented application
launcher is `python capture_and_detect.py --web ...`. Runtime dependencies are
separate from the Pi gateway requirements.

### Robot controls panel

Expand **Robot controls** in the same dashboard. These explicit controls do not
use the language model or a cup detection. In live mode the app reads the
gateway's `/openapi.json` at startup and when **Refresh robot status** is used.
Only actions supported by that contract and their required status checks are
shown. Unsupported actions are also rejected by the backend before obtaining
control or writing recovery records. If discovery fails, movement controls stay
hidden and Refresh remains available to try again. Demo/dry-run controls are
previews and do not discover or contact hardware.

The current Lynx C++ gateway (API version 0.4.0) supports Home, grippers,
status and software Stop. Its missing collision-status route is supplied by
the read-only controller connection described below. Collision acknowledgement,
guarded reset and guarded initialization are hidden on that gateway. Its legacy
`POST /robot/rapid/start` with `reset_program_pointer=true` starts execution from
main; it does not provide the reset-and-leave-stopped contract or the generation
check that prevents a delayed initialization from restarting after Stop.
The dashboard does not use it as a substitute for those actions.

Controls available according to the connected gateway:

- **Movement speed** sets normal speed for future cup plans, from 1 to 1500 mm/s,
  and caps sequence homes at `min(selected speed, 200)` mm/s. Cup flips remain
  capped at 100 mm/s. Without an override, startup speeds are normal 250,
  sequence home 200, and cup flip 100 mm/s; all three are shown in the panel
  and cup confirmation. Applying the current normal speed still updates the
  home cap when needed. It cancels pending selections and lasts until the app
  restarts. It does not change a movement already running or the FlexPendant
  speed override.
- **Home left / right / Right, then left home** reviews a home request without needing a scan.
  Both runs right first, then left, only after the first arm's completion and
  measured home joints are confirmed. Manual home speed is capped at
  `min(selected speed, 100)` mm/s, separate from homes within the cup sequence.
- **Open / close grippers** operates the selected arm or both, sequentially.
  Opening a gripper can release a held cup. These controls do not reposition it.
- **Refresh robot status** refreshes available controls and reads RAPID execution, motor state, operation mode,
  and collision acknowledgement state. It is a timestamped snapshot. Demo and
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
- **Acknowledge collision** requests the controller's acknowledgement after
  inspection. It does not start RAPID, turn motors on, or move home.
- **Reset robot program (leave stopped)** stops execution, clears and verifies pending arm request
  flags, and resets the program pointer to main. It leaves RAPID stopped.
- **Initialize robot program** explicitly clears pending requests and starts at main,
  rather than resuming an interrupted instruction. The arm programs may
  initialize/calibrate the grippers. AUTO, motors already on, and a cleared
  collision state are required; the app does not switch modes or enable motors.

Each robot action has a separate, one-use 60-second preview and inspection
checkbox. No additional action is queued while one is running. Stop invalidates
both cup and robot-control previews; a failed/unconfirmed command requires
inspection again. Manual robot actions invalidate the scan. They never free
reserved placement locations or automatically continue an interrupted pick.

After a collision, inspect both arms, the cup and the return path in person.
If they are trapped or in contact, use the robot's recovery procedure; Home is
not an escape planner. Acknowledge the app block, refresh status, and request
collision acknowledgement if needed. Reset and initialize are separate reviewed
actions. Once the controller is ready and the path is clear, review a Home
request for the appropriate arm. Nothing automatically resets and homes.

ABB's [collision acknowledgement API](https://developercenter.robotstudio.com/api/rwsApi/panel_ack_set_page.html)
requires **Local Client privileges** and disabled automatic acknowledgement.
The Pi gateway may be refused. In that case use the FlexPendant; this app does
not change privileges or bypass collision supervision. Motor enable, emergency
stop recovery and other controller faults can also require physical controls.

**Controller status on Lynx:** `--controller-profile` defaults to the adjacent
`robot_controller.json`. This explicit profile binds gateway
`http://192.168.125.20:8000/api/v1` to controller `http://192.168.125.1`.
It contains URLs only. An absent profile or a different gateway URL does not
enable the native connection. Use a separate verified profile when changing
the installation; the app does not guess a controller from the gateway address.
Native reads use ABB Digest authentication and the gateway's existing credential
defaults, with `GATEWAY_RWS_USER` and `GATEWAY_RWS_PASSWORD` overrides.

Before each manual home/gripper command, the app reads both arms' six joints and
arm angle from the gateway and native controller, requiring agreement within
0.1 degree. It then reads `/rw/panel/coldetstate`. These are GET requests only;
this helper cannot reset/start/stop the robot, acknowledge faults, or enable motors.
The joint comparison checks consistency, not unique controller identity.
Collision must be `INIT` or `CONFIRMED`, with RAPID running, motors on and AUTO.
Missing data, a mismatched joint reading, a failed request or another collision
state blocks the command. When the gateway advertises its own collision-status
route, that route is used; failure does not trigger a native fallback.

**Deployment:** restart the Mac dashboard after the robot is idle to load these
UI/backend changes. A browser refresh alone does not reload Python code.
Guarded reset/initialization and collision acknowledgement need those routes
implemented in the installed gateway package. The Python gateway in this
checkout provides them; updating its source does not update the Lynx C++ package.
For a Pi Docker deployment see
[`gateway/README_START.md`](../gateway/README_START.md#deploy-to-the-raspberry-pi).
Do not update/restart either gateway or dashboard during robot movement.

## Local language commands

The language interface accepts typed sentences in the terminal or web dashboard.
Voice recording, speech recognition, and spoken replies are not implemented. The architecture is:

```text
Operator sentence -> local Ollama model -> validated action + cup selector
                  -> displayed proposal -> operator confirms exact proposal
                  -> existing CupSession camera checks -> existing robot sequence
```

The default model is `qwen3-vl:4b-instruct`. This vision-language model also accepts
text-only requests, so it replaces the separate text model for command interpretation.
Ordinary language commands send text and scene metadata. Saved-cup requests also
send the saved reference photo and current raw cup crops to the local model.
Actual RAM use varies with context; performance must be checked on the M4 MacBook.
Both use a 4096-token context. Visual matching sends one labelled comparison
image containing the reference and every candidate, reducing local inference time.
Both disable thinking output.
[Model details](https://ollama.com/library/qwen3-vl:4b-instruct).

### Save a cup and request it by name

Run camera mode with `--llm --llm-model qwen3-vl:4b-instruct`. In the web app,
scan, select a cup, enter a name such as **Kasun mug**, and choose **Save cup**.
The **Saved cups** panel keeps the reference photo across scans and app restarts.
**Find & preview pick** takes a new scan and compares its cup crops with the
reference using the local VLM. Review the saved photo and highlighted current cup,
then confirm the existing pick/handover/place/home sequence.

The same commands work in the web command box and terminal:

```text
scan
save cup 2 as Kasun mug
saved cups
pick saved Kasun mug
```

`pick Kasun mug` is also accepted when the entire name exactly matches a saved
entry. Names are case insensitive. Saving never moves the robot. A named pick
always needs confirmation, including in the terminal (`confirm <code>`).
`forget saved Kasun mug` or **Forget** removes the reference without moving.
Duplicate names are rejected; forget the old entry before saving a replacement.

References live in `Data/Detections/saved_cups.json` under the configured `--output`
directory. Each contains a cropped raw RGB photo and capture metadata, not robot
coordinates. There are at most 32 references, with names up to 48 characters
(letters, digits, spaces, underscores and hyphens; start with a letter). Use the
same output directory after restart to access the same library. The demo does
not create references; use camera dry-run mode to try the real image workflow.

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
the existing **single pre-motion verification**, the VLM checks the fresh geometric
target against the same reference. It may reject that target, but cannot redirect
the pick to another cup. There is still no second camera check at ready. Model
processing counts toward the existing 15-second image-age limit; a slow visual
check blocks rather than using stale coordinates. Stop remains independent of
model processing. Numeric picks and the robot sequence are unchanged.

Images are sent only to the configured local Ollama endpoint using its
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
python capture_and_detect.py --check-llm
```

This sends a synthetic “Hover above cup two” request to local Ollama. It does not
load YOLO, request a gateway token, connect to the camera or contact the robot.
It checks whether the model returns the expected intent for that example. It is
not a complete language accuracy evaluation.

Then try actual camera observations in dry-run:

```sh
python capture_and_detect.py --gateway http://192.168.125.204:8000 --llm --dry-run
```

```text
cups> scan
cups> pick cup two
Interpreting locally ...
Preview only: cup 2 — pick with the left arm, hand to the right, place, and return both arms home.
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

Exact existing terminal commands such as `goto 2` and `hover 2` remain direct
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
Every confirmed movement still uses the initial camera verification, workspace and
height validation, persistent recovery lock and gateway lease. The model's
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

Web tests use a fake camera, fake robot or isolated demo and never contact hardware
or Ollama. HTTP tests skip if FastAPI/httpx are not installed.

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
matching-model setup, dry-run disconnection experiment, and gateway/session
requirements before live use. The coordinator and authoritative robot state
stay on the Pi throughout switching and recovery; Lynx is not needed.
