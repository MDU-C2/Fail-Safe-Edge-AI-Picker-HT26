# Select a cup and run the handover

The original `capture_and_detect.py` is restored exactly to the repository version
at Git commit `ddec38c`. Our developed version is preserved separately as
**`capture_and_detect_custom.py`**, with its existing helper modules. The web app
imports this custom version. The instructions below describe the custom version.

The restored original automatically attempts to pick a cup after Enter; it does
not provide this version's scan/selection, LLM, dry-run or recovery-journal workflow.
Both scripts use `Data/Detections`, so run only one controller at a time.

`scan` detects cups and saves a numbered inventory. **`goto 2` now picks cup 2
with the left arm, hands it to the right arm, places it at the existing drop
position, and finishes with both arms home.** `hover 2` retains the previous
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
... verification image and all 23 planned requests, no robot movement ...
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
of picking. Keep the fixed right-hand placement position clear before each cycle.

| Command | Behavior |
| --- | --- |
| Enter / `scan` / `capture` | Capture and save all candidates; no movement. |
| `list` | Show IDs, coordinates, corrected hover positions and blocked reasons. |
| `show 2` | Show the complete saved observation and execution record for cup 2. |
| `goto 2` | Verify, pick, hand over, place, and home both arms. |
| `hover 2` | Verify, move the left arm through ready, verify again, hover only. |
| `reset` | Acknowledge manually checking/recovering the robot; no hardware command. Requires a new scan. |
| `q` / `quit` | Exit; does not stop a command already dispatched. |

After a live action, run `scan` again. `reset` is for recovery after an interrupted
or unconfirmed action; it is not needed after a successful cycle and is not a
command to move home.

## What goto does

The plan in `handover_plan.py` reuses the poses from the repository's original
`capture_and_detect.py` handover. Jobs run sequentially at **50 mm/s** by default.
Every job must return `accepted: true` and exactly `message: "DONE"` before the
next starts. `--speed` changes movement/home speed (1–1500 mm/s); acceleration
and ramp remain 100 percent.

1. Home the right arm and check its measured joint positions.
2. Move the left arm to ready and open its gripper.
3. Recheck the selected cup, approach above it, descend, grip, and lift.
4. Hold it at the existing left-hand handover position.
5. Open the right hand, move via the existing approach positions, and grip.
6. Only after the right grip reports completion, release the left hand and lift it clear.
7. Lower and turn the cup with the right arm. Once it clears the handover,
   home the left arm and check its joint positions.
8. Lower the right hand to the existing placement position, release, lift clear,
   then home the right arm and check its joint positions.

`DONE` confirms a gripper command completed; the API does not prove the fingers
actually hold a cup. This workflow has no independent grasp or placement sensor.

## Coordinates and workspace

The saved transform remains `robot XYZ = scale × R × camera XYZ + t`.
The left cup approach, descent and lift add calibration `tag_offset` X/Y once.
The detected base Z is checked, but heights remain fixed for this tabletop:

| Position | Gripper TCP Z | Flange Z sent to RWS |
| --- | ---: | ---: |
| Above selected cup | 250 mm | 409 mm |
| Pick selected cup | 75 mm | 234 mm |

The 159 mm difference is the existing gripper length. `tag_offset[2]` is not
added to these fixed heights. For example, stored X=600 and offset X=-115 gives
commanded X=485 for approach, pick and lift.

Fixed poses are already flange positions; no camera offset is added to them:

| Pose | Flange X/Y/Z (mm) |
| --- | --- |
| Left ready | 400, 100, 450 |
| Left handover / lifted clear | 400, -100, 549 / 584 |
| Right via | 200, -250, 200 |
| Right beside / below / take | 380, -235, 175 / 380, -115, 175 / 380, -115, 210 |
| Right above placement / placement | 420, -200, 310 / 230 |

Left rotation is `(180, 0, 180)`. Right rotation is `(0, 0, 0)` for receiving
and `(180, 0, 0)` for placement. Home uses the existing RAPID home routines;
the client also checks all seven reported axes within 1 degree of those targets.

Saved XY limits remain X=350–800 and Y=-230–420 mm. Corrected flange targets
must also fit the gateway defaults: X=180–800, Y=-400–500, Z=50–600 mm.
Full picking additionally requires a detected cup-base Z of 10–150 mm.
A candidate permitted only for hovering is labelled `HOVER ONLY` in the image
and `hover only` in the list. A cup with Y=-627 mm remains blocked even when
explicitly selected. Place it inside the calibrated work area and scan again;
selecting an ID never bypasses coordinate checks.

These are endpoint checks, not path or collision guarantees. The existing
controller plans joint movement; this client does not turn it into a guaranteed
straight-line path. The fixed drop position is not automatically checked for
occupancy. The workflow assumes the same cup size and tabletop setup as the
existing handover code.

## Verification, interruption and recovery

The selected cup must match a fresh observation uniquely within **20 mm in 3D
and 40 pixels**, with unchanged calibration and advancing camera sequences.
A second check at left ready updates all cup-relative coordinates before the
approach. Other cups' IDs in verification images never change the user's selection.

Image age is checked before starting, approaching, descending and gripping.
The default **15-second** deadline accounts for approach and descent at 50 mm/s;
`--max-result-age` changes it. Capture, inference, saving and movement time all
count toward that deadline. If a slow approach exceeds it, descent or grip is
blocked. After gripping, the fixed handover continues without requiring the cup
to remain at its original camera position. This is not continuous scene monitoring.

Camera errors, invalid/missing/ambiguous targets, failed persistence, failed
commands or a lost control lease stop further dispatch. No automatic retry,
release, return-home or reverse sequence is attempted after failure. An already
sent command may still finish. `reset`, quitting, Ctrl-C and lease expiry are
not hardware emergency stops.

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
gets a limited scene summary and the active scan image. Host/origin and request-token
checks reject requests from unrelated websites. There are no remote assets or
external UI dependencies. Use `--port 8081` if port 8080 is already occupied.

**Cancel request** only discards a proposal. Closing the page, losing its connection,
or stopping this server is not an emergency stop. The UI does not add a gateway or
robot watchdog. Independent robot stopping controls remain necessary. Recovery
acknowledgement requires an explicit inspection checkbox and sends no robot command;
it clears the recovery lock and requires a new scan. Do not acknowledge it until the
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

## Local language commands

The language interface accepts typed sentences in the terminal or web dashboard.
Voice recording and speech recognition are not implemented yet. The architecture is:

```text
Operator sentence -> local Ollama model -> validated action + cup selector
                  -> displayed proposal -> operator confirms exact proposal
                  -> existing CupSession camera checks -> existing robot sequence
```

Start with `qwen3:4b` on the M4 MacBook. Its current Ollama package is approximately
2.5 GB; actual RAM use is higher and varies with context. It is a starting point
for this small command task, not a measured performance guarantee on your machine.
The code limits model context to 4096 tokens and disables thinking output.
[Model details](https://ollama.com/library/qwen3:4b).

Install and open [Ollama for macOS](https://docs.ollama.com/macos), then run:

```sh
ollama pull qwen3:4b
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
| `hover above cup two` | Propose hover only. |
| `hover above the rightmost cup` | Resolve rightmost in the camera image, then propose or block. |
| `pick the blue cup` / `go to that one` | Ask for an explicit supported reference; no color/gesture data exists. |

Leftmost and rightmost include all detected candidates, even blocked ones. A
blocked edge cup is not replaced by another cup. “Leftmost available,” nearest,
colors, gestures, multiple cups and automatic multi-action plans are not supported
in this version; use an explicit cup ID. The model is prompted to clarify such
requests, but the operator must still inspect every proposal for misinterpretation.

Exact existing terminal commands such as `goto 2` and `hover 2` remain direct
operator commands and do not go through the LLM or proposal step. `scan`, `reset`,
direct movement, quitting, or a new language request invalidates a pending
proposal. `list`, `show 2`, and `help` can inspect it without invalidating it.
The exact word `cancel` discards it. `stop` also only cancels a pending proposal:
it is **not** an emergency stop, and input is not processed during an active
synchronous robot sequence. Keep hardware stopping independent of language.

The local model receives only IDs, image positions, status, and availability.
It cannot send XYZ, arbitrary code, gripper commands, home, reset, speed or
calibration changes. Its JSON output is independently validated; the app resolves
relative references, binds the exact capture, and rejects stale proposals.
Every confirmed movement still uses the original camera rechecks, workspace and
height validation, persistent recovery lock and gateway lease. The model's
explanation is labelled separately; success messages come from actual execution.
Schema-valid output can still choose the wrong cup: confirmation is not optional.

The default endpoint is `http://127.0.0.1:11434`. `--ollama-url` accepts loopback
addresses only; redirects and environment proxies are disabled. `--llm-model`
selects another installed local model. Timeout, malformed output or a missing
model cancels the old proposal and never causes fallback motion. The app does not
download models automatically. Offline operation assumes Ollama loads the selected
model locally; the supported starting configuration is downloaded `qwen3:4b`.
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
