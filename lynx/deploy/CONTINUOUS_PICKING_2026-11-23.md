# Continuous browser picking and queue completion saves

New browser **Pick all** confirmations now authorize continuous picking until
Stop. The shared run carries `settings.continuous_batch: true`; Pi and Lynx
follow that setting when taking over. Existing admitted finite runs and the
terminal workflows retain finite behavior. Installing this update does not
restart a completed, stopped, blocked or recovered run.

After each cup, both existing parking movements complete before one fresh
workspace-and-grid observation. Moved and newly added cups refresh the next
pick; observed empty grid cells become available. When the workspace is empty,
the arms go Home once and keep watching. A full grid with cups still in the
workspace waits for a cell without going Home. The one-second interruptible
idle polling interval and the five depth samples are unchanged.

The coordinator advertises `continuous_batches`. Its durable `watching` phase
requires both measured Home completions and retains their receipts. Admitting
the next cup starts the next Home cycle atomically. A returning worker takes a
fresh scan, adopts the parked state and avoids repeating Home. Historical Home
receipts retain their measured evidence, but cannot confirm a later cycle.
Stop and inspected failure recovery remain terminal for that shared run.

## Where the observed delay occurred

In the inspected November 23 recording, the gateway reported the final cup
queue complete at `09:35:32.093126`; the next captured sample was timestamped
`09:35:35.358984`. Both timestamps use the gateway clock: approximately 3.27
seconds elapsed before that captured frame. Five samples then spanned about
1.00 second. Capture and processing took approximately 2 seconds in the latest
records, with detection around 0.5–0.7 seconds. Comparable November 20 records
were approximately 2.2–2.4 seconds. These records point to the gap before capture,
rather than a slower model or repeated inference.

The queued executor previously replayed every completed step's bookkeeping
after receiving the entire cup's terminal result. For a 19-step successful cup,
its completion ledger alone required 39 atomic file writes and 78 file/directory
fsync calls on macOS. The new explicit aggregate callback path requires two
ledger writes and four fsync calls. CupSession also saves the prepared plan and
completed result once per phase instead of once per step.

The complete plan is durable before the robot POST; the callback attempt is
durable before the completed session is saved. Completion is marked applied
only after that save succeeds. Errors or Stop preserve incomplete callback
evidence and do not replay callbacks. Failed/cancelled partial queues, Home
measurement, unbound queues and callers without aggregate callbacks retain
their existing per-step handling. Coordinates, speed, queue groups and robot
commands are unchanged.

Offline tests cover multiple continuous cycles, additions, cleared cells,
watcher takeover, interrupted Home, old finite runs, stopped/recovered runs,
exact historical Home evidence, bulk persistence errors and concurrent Stop.
The measured write-count reduction does not establish a new physical cycle
time; that requires a subsequent operator-run picking trial.

## Installation

MacBook uses the repository source. The Pi and Lynx payload replaces the shared
`cup_commands.py`, `failover_workflow.py`, `failover_coordinator.py` and
`queued_robot_client.py`, with backups and only their manifest entries updated.
The gateway executable, RAPID, handover plan, model, calibration and saved grid
are unchanged. Deployment evidence is in the workspace's
`tmp/continuous-picking-update-20261123` directory.

Start and confirm a new browser batch to use continuous watching. No batch is
started automatically during installation, and no recovery is requested.

Deployment status: **installed after the operator stopped the other picker**.
Lynx and Pi were staged and verified offline in their existing Python runtimes,
then their armed services were restarted. All four installed source hashes match
the tested payload. The MacBook UI was reloaded with its existing launch settings
and reports `motion.continuous_batch: true`. All three roles subsequently reported
advancing `armed` heartbeats with no active run. The previous completed shared
run and its head remain unchanged.

Backups are stored at:

- Pi: `/home/kasun/picker-devices-20261113/continuous-picking-update-20261123/backup`
- Lynx: `/state/cli-0.2.0/continuous-picking-update-20261123/backup`

The operator's stop left a terminal cancelled queue: the initial two gripper-open
steps completed and every motion step was skipped. Installation allowed only that
exact reviewed queue as an additional idle case, required no control owner and
both arms reporting DONE, and preserved the queue unchanged. Pi's 12 and Lynx's
15 protected state fingerprints, Mac pending/calibration/grid/recovery records,
and all motion fingerprints were preserved. No movement, capture, recovery,
queue clearing or batch start was requested during deployment.

The dashboard now exposes its existing inspection/recovery panel for an idle
failed or cancelled gateway queue even when there is no local recovery journal.
The acknowledgement remains mandatory. A digest of the complete queue readback
binds the checkbox to the observed queue; a replacement queue or changed control
state clears it. This change only affects read-only status and dashboard gates;
recovery semantics and the gateway are unchanged. A browser check confirmed the
panel is visible, its acknowledgement unchecked and its action disabled until
inspection. The cancelled queue still requires that explicit recovery before a
new batch can start. Refresh the browser, inspect and unblock, then scan and
confirm a new batch.

Verification completed locally: 646 Python tests plus 245 subtests in the broad
suite, six additional integrated callback tests, and 161 dashboard tests. The
final queue-identity adjustment passed 43 active-device Python tests and 25
targeted dashboard tests. Both remote candidate verifiers passed finite and
continuous cycles, cleared-grid waiting and watcher takeover without repeated
Home, with real network and model startup prohibited. A physical picking trial
has not been run after installation.

Local deployment evidence includes `pi-deployment-result.txt`,
`lynx-deployment-result.txt`, `mac-deployment-result.json`, and
`final-all-device-readiness.json` in `tmp/continuous-picking-update-20261123`.
