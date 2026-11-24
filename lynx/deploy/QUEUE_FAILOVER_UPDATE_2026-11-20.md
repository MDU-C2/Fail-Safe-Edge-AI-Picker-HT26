# Lynx finite queue failover — 2026-11-20

## Browser-started three-device mode

The opt-in browser workflow now uses `failover_workflow.py` on all three
devices. MacBook is `primary` (300), Raspberry Pi is `secondary` (200), and
Lynx is `tertiary` (100). A returning higher-priority worker takes the next cup
after the gateway finishes its accepted queue. Each acquisition begins with a
fresh workspace/grid observation. A completed batch remains completed; armed
workers wait for another explicit browser confirmation without capturing.

The separate `failover_coordinator.py` service runs on Lynx port 8092. It uses
read-only gateway requests and stores batch authorization, exact queue receipts
and independently verified Home progress. This preserves completion evidence
when a successor replaces the gateway's latest queue. It does not change the
gateway or RAPID, acquire control, or send movement commands. Receipt polling
and worker heartbeats do not repeatedly rewrite unchanged durable state.

Enable the MacBook's local device configuration with:

```json
"failover": {
  "coordinator_url": "http://192.168.125.20:8092",
  "token_file": "/private/location/coordinator-token"
}
```

The local gateway credential must be the primary role. Credentials stay in
private files/environment and are excluded from source and deployment archives.
Manual device switching is disabled in this mode; the local browser continues
to provide selection and Assistant functions, while gateway priority chooses
the batch worker. Automatic continuation applies to confirmed **Pick all**
batches. Individual selections remain individual operations.

Pi uses its existing Python environment, model and state with
`capture_and_detect/failover_worker.py --live`; Lynx uses
`python -m lynx.picker --live --armed`. Both require `--coordinator-url` and
`--coordinator-token-file`, plus their usual gateway, model and state arguments.
The earlier one-shot `--standby` interface remains available separately.

The armed deployment uses a persistent source overlay on the installed Lynx
0.2.13 image at `/state/cli-0.2.0/armed-runtime-20261120/app`. Its existing native
model and binary remain installed. `lynx.armed_service` hosts the coordinator
listener and picker in one Python runtime; native inference remains a separate
process. This avoids duplicate Python memory while retaining the existing
128 MiB measured startup-headroom check. The inactive Lynx dashboard listener
is stopped; the MacBook browser remains the operator UI.

`/state/cli-0.2.0/start-armed-services.sh` starts this combined service, and
`ensure-armed-services.py` starts it detached if it is not already running.
`start-device-service.sh` now selects this armed launcher; its original version
is backed up under `backups/armed-launcher-20261120`. The existing WeOS
`cuppicker` boot configuration remains unchanged (manual start, idle container
command); network reconnection does not require restarting the service, but a
Lynx reboot still requires starting the container and armed launcher.

Pi's enabled `failsafe-picker-armed.service` reuses
`/home/kasun/picker-devices-20261113`, its existing `best.pt` model and state,
and `/home/kasun/venvs/failsafe-yolo/bin/python`. Its user service has lingering
enabled, so it survives SSH logout. Calibration, grid state and model files
were preserved on both devices.

Live readiness was verified with fresh `primary`, `secondary` and `tertiary`
heartbeats, all `armed`, and no admitted shared batch. Lynx passed its unchanged
memory guard with 138.7 MiB cgroup headroom and loaded its native model. The
combined Python process used about 32 MiB RSS and the native worker about
64 MiB RSS; no new OOM events occurred. All 38 deployed overlay files matched
their source hashes, and no startup camera capture was created. Pi's model
finished warming and its enabled user service remained active across SSH logout.

The full Python suite passed 2,734 tests before the final focused changes;
229 affected failover/UI tests and 9 browser status tests then passed. The
combined Lynx launcher and related native/coordinator tests passed 135 tests
plus 13 subtests, and its packaging tests passed separately. Physical unplug
and picking tests were not initiated by this deployment.

A separate legacy picker was subsequently started by the operator from
`/home/kasun/AnasPicker` in Pi tmux session `picker`; another primary workflow
also appeared at the gateway. These were left running. Finish the separate
workflow before starting a browser failover batch: workers sharing the same
role credential are not separate gateway identities. The legacy Pi workflow's
Ctrl+C during a queue wait can clear that queue, so do not use it to interrupt
an unfinished cup during the transition.

Unknown submissions, failed queues, unrelated recovery journals and explicit
Stop remain blocked. A transport failure never blindly repeats a cup. Closing
an armed worker ends only its local wait and lease; it does not cancel accepted
gateway work. The browser's explicit Stop ends shared batch admission and
requests the existing robot Stop independently.

The following release notes describe the earlier package installation and
one-shot standby behavior, before this opt-in armed deployment.

Release **0.2.13 is installed on Lynx** and adds native standby behind the existing gateway's cloud
and Pi priorities. It uses the existing C++ gateway 0.6.0 queue API. Gateway
source, its running process and the robot RAPID program are not replaced.

## Workflow

- Cloud uses priority 300, Pi 200, Lynx `tertiary` 100. Lynx waits for control,
  then waits for any already accepted cup queue to finish. It does not repeat
  that cup or retry a submission whose outcome is unknown.
- A whole cup's shared smooth handover, placement and camera parking is submitted
  once as a gateway queue: 19 jobs, including 12 buffered paths. Dependency
  groups preserve each arm's order and the handover barriers.
- The left uses the existing left parking target. After placement clearance,
  the right uses the normal upright `right_via` target before capture. A
  successfully completed inherited cup lacking that final right parking job
  receives the same parking tail, with recovery journaling.
- A fresh native observation refreshes workspace cups and grid occupancy after
  each completed cup. Moved and newly added cups are planned from their current
  coordinates. Removing a grid cup frees its observed-clear cell for reuse.
- While pickable workspace cups remain and the grid is full, the arms stay at
  their clearance poses and the camera checks periodically. A clear cell resumes
  picking. A full grid alone never sends the arms Home.
- An empty workspace sends the right arm Home, verifies its measured joints,
  then sends and verifies the left. The batch finishes without another capture
  or watching for new cups. Visible but unpickable source cups prevent Home.
- Individual selections remain one cup, with one following scene refresh and
  no batch loop or automatic Home.

Lynx holds one lease throughout its batch, including captures and grid waits.
The updated `capture_and_detect/capture_and_detect_que.py` also holds one lease
through a primary capture/detection/batch/Home sequence. Use that updated file
on cloud and Pi; older primary copies can release control between cups while
the program is still working. No Pi or cloud runtime was remotely replaced in
this deployment.

The existing Mac UI retains its continuous-batch default. Native Lynx explicitly
sets `continuous_batch=False`, independently of workspace/grid refresh.

## Launch

Connect to Lynx and enter its picker container:

```text
ssh admin@192.168.125.20
app-hosting
attach cuppicker
```

Then arm one takeover batch:

```sh
sh /state/cli-0.2.0/start-queue-standby.sh
```

The launcher executes the equivalent command:

```sh
/start.sh --gateway http://127.0.0.1:8000 \
  --state-dir /state/cli-0.2.0 \
  --token-file /state/cli-0.2.0/tertiary-token \
  --live --standby
```

The supplied tertiary credential is stored separately from the existing UI
gateway credential. Credentials are excluded from source, packages and this
report. The gateway must already have that role configured; the picker never
edits its authentication configuration.

A state-directory process lock prevents the native service and standalone picker
from loading competing workers. Leave the Lynx dashboard controller inactive
when starting standalone standby. The dashboard service can remain online.
Ctrl+C or process termination in standby sends no new commands or automatic
queue cancellation; an already accepted cup may still finish at the gateway.
Ordinary interactive CLI Stop remains the explicit software-stop operation.

## Installation and validation

The `lynxcli` label now selects the valid 0.2.13 image. Only `cuppicker` was
reloaded. The original dashboard-service launcher restarted its service on
port 8091, with `controller_present=false`, `ready=false` and no model worker.
The WeOS `copy run start` command returned without error; a reboot was not tested.

All 41 installed application/model/startup/support files matched the final
source manifest. The same standalone offline verifier passed on Lynx with real
HTTP, socket and subprocess calls blocked. All 162 backed-up non-image state
files remained byte-for-byte unchanged, including after the service restart.
Existing calibration, grid, credentials, motion settings and recovery were
preserved. The new tertiary credential and manual standby launcher are separate
files. Captures were not taken during installation.

The native worker hash remains
`88c661ac80d3a32b6ad2c791d29bd59145bac781cedd771bfe63d47c5c3bad27`
and the model binary hash remains
`3d1167d533a8ad907caf9673ed1374e9f2977093dc89e1ad65758a79bec93bb8`.
Gateway 0.6.0 uptime increased to 258,390.4 seconds without a restart. Its failed
queue was unchanged, no control lease was acquired, and no robot command was
sent. The picker service boot ID is `75869792aecf4725ba0e829403f1b09d`.

The Python suite passed 2,562 tests before the final two ownership regressions;
the final affected suites passed with those regressions included. All 137
dashboard tests passed. Local HTTP tests used mock loopback servers, not the
robot. `git diff --check` passed.

`verify_queue_runtime.py` passed in the ARM64 image with Docker networking
disabled. It uses the actual session and queue client against in-memory camera
and gateway doubles, isolated temporary state, and blocked HTTP, socket and
subprocess creation. It verified moved/new cups, three uses of one freed grid
cell, a full-grid wait, separate verified Home queues, initial grid-only
completion, exact queue identity/completion, wrong-role rejection and no replay
after a lost submission response. It loads no model worker or heavy Python
image/ML libraries.

| Item | Value |
| --- | --- |
| Release | 0.2.13, Linux ARM64 CLI profile |
| Package bytes | 47,474,106 |
| Package SHA-256 | `d49b5e8519c2b9bc651f483d7804eacd4d71e77fc575343441d4818f90fb6da2` |
| Image ID | `sha256:901ce7fc1a57fd2f04f4073502ce6ab24723f5e7301f79c55b96aa80e430bee7` |
| Source-manifest SHA-256 | `cd69b0d1f455aad9de04dbc1df6640cbec0cd1d532219b356a35082d53deb2ce` |
| Plan fingerprint | `6d380698e0d4a869db57f81367c12bcac37c9f12c129ef475612639b017df263` |
| Signature | Unsigned local package, accepted explicitly at import |
| Persistent state | `/state/cli-0.2.0` |

Physical picking, camera clearance and unplugging a live Pi are not exercised by
these offline checks. The gateway currently retains a failed primary queue:
`left_park` exceeded 60 seconds before right-arm placement. This record is
preserved, and standby refuses to continue from failed or cancelled queue
history. Inspect and recover that unfinished handover before arming standby.

Installation evidence and the pre-update backup are kept under
`/state/cli-0.2.0/backups/queue-0.2.12-20261120/`. The directory name identifies
the initial backup; the final release is 0.2.13. Automatic standby is not started
by package installation or by the existing dashboard-service launcher.
