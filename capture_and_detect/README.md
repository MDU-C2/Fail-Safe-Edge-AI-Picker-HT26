# Select a cup and run the handover

The upstream `capture_and_detect.py` remains separate and unchanged by this integration.
Our developed version is preserved as
**`capture_and_detect_custom.py`**, with its existing helper modules. The web app
imports this custom version. The instructions below describe the custom version.

The upstream original automatically attempts to pick available cups after Enter; it does
not provide this version's scan/selection, LLM, dry-run or recovery-journal workflow.
Both scripts use `Data/Detections`, so run only one controller at a time.

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
Calibration defaults to `../calibration/calibration.json`, relative to the script.

Natural-language input is available with `--llm`; see **Local language commands**
below. It uses an offline Ollama model on the Mac and the same cup-selection checks.

## Run

From the `capture_and_detect` directory on the Mac, preview the whole sequence:

```sh
export GATEWAY_TOKEN="BAD_TOKEN_OPERATOR"
python3 capture_and_detect_custom.py --gateway http://192.168.125.204:8000 --dry-run
```

Use the configured operator token if yours differs. The Pi address above is the
current lab address; update it if the Pi's address changes. The default address
without `--gateway` remains `http://169.254.125.202:8000`.

```text
cups> scan
cups> list
cups> show 2
cups> goto 2
... verification image and all 24 planned requests, no robot movement ...
cups> quit
```

For live operation, restart without `--dry-run`:

```sh
python3 capture_and_detect_custom.py --gateway http://192.168.125.204:8000
```

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
| `hover 2` | Verify, move the left arm through ready, verify again, hover only. |
| `goto all` / `pick all available cups` | Preview available cups in confidence order, limited by free grid locations. |
| `confirm <code>` | Execute the exact batch or language proposal, once, within 60 seconds. |
| `cancel` | Discard a pending proposal; does not stop robot movement. |
| `reset` | Acknowledge manually checking/recovering the robot; no hardware command. Requires a new scan. |
| `q` / `quit` | Exit. If terminal work is active, request software stop before waiting for it to unwind. |

After a live action, run `scan` again. `reset` is for recovery after an interrupted
or unconfirmed action; it is not needed after a successful cycle and is not a
command to move home.

## Start, software stop, status and help

The web dashboard and custom terminal accept these exact commands without an LLM:

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

The plan in `handover_plan.py` reuses the poses from the repository's original
`capture_and_detect.py` handover. Normal moves use **250 mm/s** by default,
with **100% acceleration and ramp**. Automatic home is capped at **200 mm/s**.
Picking, lifting and carrying to the handover are capped at **100 mm/s**.
The two cup-turning moves use **100 mm/s**
and **30% acceleration**, with 100% ramp, matching the upstream program.
Every job must return `accepted: true` and exactly `message: "DONE"` before the
next starts. `--speed` changes normal movement speed (1–1500 mm/s);
home, picking and turning use the lower of that setting and their respective caps. Actual speed also depends
on the robot controller. The custom controller retains serial confirmed jobs,
its initial right-arm home check and one camera verification per cup; it does not
adopt the upstream program's concurrent arm timing.

1. Verify the selected cup and destination with a fresh camera observation,
   then home the right arm and check its measured joint positions.
2. Open the left gripper, then move the left arm to ready.
3. Approach the verified cup position, descend, grip, and lift.
4. Hold it at the existing left-hand handover position.
5. Open the right hand, move via the existing approach positions, and grip.
6. Only after the right grip reports completion, release the left hand and lift it clear.
7. Lower the right arm, turn sideways at the midpoint between the lowered
   receiving pose and the raised destination, then turn downward above that
   destination. Once it clears the handover,
   home the left arm and check its joint positions.
8. Lower the right hand to the selected grid location, release, lift clear,
   then home the right arm and check its joint positions.

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

The upstream update also adds cup-load handling in `rapid/RightArmInstructions.mod`.
That behavior runs on the robot controller when its updated RAPID module is loaded;
changing the Python files alone does not install it. The custom app uses the same
right-gripper grip/open requests. This integration does not deploy or modify RAPID.

## Placement grid and batches

Both custom entry points default to `placing_grid.json` next to the script;
`--placing-grid /path/to/grid.json` selects another file. Locations are flange
coordinates, used without camera offsets. The next location is the first free
entry in file order that is free and not blocked by an uncertain camera check.

### Camera grid and automatic release

Restart the custom terminal or web app after updating, then use **Scan workspace**
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
The chosen destination is checked in the single fresh verification before motion;
occupied, unknown or missing evidence blocks the sequence. No second camera
check runs at left ready.
These checks do not observe the entire transfer continuously.

The overlay uses placement **X/Y and table Z=0 mm**, not flange Z=230 mm.
It inverts the calibration using all three tag-offset components. Cells are
80 mm square, matching the existing spacing. `--grid-table-z` and
`--grid-cell-size` configure those measurements in both custom entry points;
changing the camera position still requires a new camera calibration.
The depth check requires 99.5% valid coverage and table agreement within ±20 mm,
with connected elevated/invalid patches blocking clearance. It checks the
projected volume up to 160 mm above each cell to account for cup-rim parallax.
This checks visible space at scan time; it does not verify grasping or guarantee
the area remains clear after the scan. Raised depth belonging to an unrelated
object beyond a cell's checked volume does not mark that cell occupied. The
calibrated camera rays, including the pixel corners and a 20 mm physical margin,
must confirm that the surface is behind the entire volume. Objects inside it,
foreground objects hiding it, and uncertain depth still prevent clearance.
YOLO cup-box overlap remains conservative, so adjacent cups can block more than
one cell when their projected areas overlap. Occupied or unknown locations
elsewhere do not block a pick into a different, verified clear destination.

`goto 2` and `pick cup 2` select only cup 2. To pick several cups:

```text
cups> scan
cups> goto all
... cups and destination locations, normal/turning speed, confirmation code ...
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
To recover a **reserved** slot, stop controllers while the robot is idle, inspect the arms and
placement area, remove any cup from that slot, then set its `occupied` to `false`
and remove its `reserved` and `reservation_id` fields. Preserve its ID and coordinates.
Restart and scan afterward; the camera must confirm its visibility before selection.
Do not edit the grid during a sequence. Run only one robot controller: the upstream
script does not participate in the custom controller's file locking.

## Coordinates and workspace

The saved transform remains `robot XYZ = scale × R × camera XYZ + t`.
The left cup approach, descent and lift add calibration `tag_offset` X/Y once.
The detected base Z is checked, but heights remain fixed for this tabletop:

| Position | Gripper TCP Z | Flange Z sent to RWS |
| --- | ---: | ---: |
| Above selected cup | 175 mm | 334 mm |
| Pick selected cup | 70 mm | 229 mm |

The 159 mm difference is the existing gripper length. `tag_offset[2]` is not
added to these fixed heights. For example, stored X=600 and offset X=-115 gives
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

Left rotation is `(180, 0, 180)`. Right rotation is `(0, 0, 0)` for receiving
and `(180, 0, 0)` for placement, with `(90, 0, 0)` at the halfway turn.
Home uses the existing RAPID home routines;
the client also checks all seven reported axes within 1 degree of those targets.

Saved XY limits remain X=350–800 and Y=-230–420 mm. Corrected flange targets
must also fit the configured gateway limits: X=180–800, Y=-400–500, Z=50–700 mm.
Ordinary flange targets must be at least 199 mm high; pick/place targets may be lower.
Full picking additionally requires a detected cup-base Z of -30–150 mm.
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
This verification runs once per cup, before any robot command in its sequence.
Its coordinates are used for the pick; there is no second camera check at left
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
python3 web_app.py --demo
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
python3 web_app.py --gateway http://192.168.125.204:8000 --llm --dry-run
```

This mode performs camera verification and previews the plan but sends no robot
commands. As with the terminal dry-run, checks that normally run between physical
steps are not executed. Without `--llm`, Scan and the explicit cup buttons still
work; typed natural language requires `--llm`.

To enable the physical robot, stop the web server while the robot is idle and
start it with the explicit **`--live`** flag:

```sh
python3 web_app.py --gateway http://192.168.125.204:8000 --llm --live
```

Omitting `--dry-run` alone does **not** enable movement in the web app. The page
shows **PHYSICAL ROBOT**, and each motion still requires clicking **Confirm cup N**.
The button binds to the exact scan, cup, action and one-use confirmation token;
it expires after 60 seconds. Even typed `goto 2` creates a proposal in the web app.
The custom terminal's exact `goto 2` behavior is unchanged. A new scan or request
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

Web implementation: `web_app.py` (local HTTP and startup), `web_controller.py`
(single controller worker), and `web_static/` (self-contained browser interface).
The camera backend is imported from `capture_and_detect_custom.py`, leaving the
restored original script independent. Runtime dependencies are separate from the
Pi gateway requirements.

### Robot controls panel

Expand **Robot controls** in the same dashboard. These explicit controls do not
use the language model or a cup detection:

- **Movement speed** sets the speed for future cup plans, from 1 to 1500 mm/s.
  It cancels pending selections and lasts until the app restarts. It does not
  change a movement already running or the FlexPendant speed override.
- **Home left / right / both** reviews a home request without needing a scan.
  Both runs left first, then right, only after the first arm's completion and
  measured home joints are confirmed. Manual home speed is capped at
  `min(selected speed, 100)` mm/s. The existing cup sequence is unchanged.
- **Open / close grippers** operates the selected arm or both, sequentially.
  Opening a gripper can release a held cup. These controls do not reposition it.
- **Refresh robot status** reads RAPID execution, motor state, operation mode,
  and collision acknowledgement state. It is a timestamped snapshot. Demo and
  dry-run modes show simulated status and send no robot requests.
- **Review app recovery** leads to the existing inspection acknowledgement. It
  clears only this app's local recovery block and sends no robot command.
- **Acknowledge collision** requests the controller's acknowledgement after
  inspection. It does not start RAPID, turn motors on, or move home.
- **Reset RAPID** stops execution, clears and verifies pending arm request
  flags, and resets the program pointer to main. It leaves RAPID stopped.
- **Initialize RAPID** explicitly clears pending requests and starts at main,
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

**Deployment:** install the updated gateway code in this checkout before using
the live robot panel. The new home/gripper controls require a confirmed clear
collision status as well as RAPID running, motors on and AUTO mode; an unknown
or unreadable collision state blocks these commands. Speed applies locally.
Rebuild and redeploy the Pi Docker image using
[`gateway/README_START.md`](../gateway/README_START.md#deploy-to-the-raspberry-pi),
then restart the Mac web app. Restarting an old Docker image does not install
these routes. An older gateway is rejected for these new actions; there is no
fallback to resuming RAPID or sending raw commands. Do not update/restart the
gateway during robot movement.

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

The same commands work in the web command box and custom terminal:

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
An independent image comparison also vetoes a model-selected cup when another
candidate has a near-identical crop. This catches obvious duplicate views; it is
not a general identity guarantee across different angles or lighting.

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
python3 capture_and_detect_custom.py --check-llm
```

This sends a synthetic “Hover above cup two” request to local Ollama. It does not
load YOLO, request a gateway token, connect to the camera or contact the robot.
It checks whether the model returns the expected intent for that example. It is
not a complete language accuracy evaluation.

Then try actual camera observations in dry-run:

```sh
python3 capture_and_detect_custom.py --gateway http://192.168.125.204:8000 --llm --dry-run
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
