# Device selector deployment — 13 November 2026

The dashboard at **http://localhost:8086/** runs in **LIVE** mode. The initial
deployment selected Lynx; after the controls correction below, **MacBook remains
selected**. Read the active device in the dashboard. Choose **Processing device → Use device**, wait for the model,
then scan. Picking requires reviewing and confirming the proposal. Changing the
device does not start a pick. Switching is blocked during work or unresolved
recovery. There is no automatic fallback when a device disconnects.

| Device | Picker service | Detection | Persistent state |
| --- | --- | --- | --- |
| MacBook | `127.0.0.1:8091` | Local `best.pt`, isolated Python worker | `codex/tmp/device-dashboard-20261113/private/mac-state` |
| Raspberry Pi | `192.168.125.204:8091` | Pi-local `best.pt`, isolated Python worker | `/home/kasun/picker-devices-20261113/state` |
| Lynx | `192.168.125.20:8091` | Native C++ INT8, 640 px | `/state/cli-0.2.0` |

The selected device runs detection, depth/placement checks, planning and the
complete picking sequence. The Mac dashboard forwards actions and displays
results. All devices use the existing Lynx gateway on port 8000. The gateway
package was left running throughout this update. No robot motion was commanded
during deployment or validation.

## Home, gripper and individual-pick controls correction

The new service launchers initially omitted the old dashboard's read-only
controller-status profile. The Lynx gateway advertises Home and gripper routes
but does not expose collision status, so those controls were correctly blocked
and incorrectly hidden without a useful explanation. Each launcher now passes
its persistent `controller-status.json`: Mac/Pi bind to the LAN gateway API;
Lynx binds to its loopback API. All use the existing controller at
`http://192.168.125.1`. Read-only checks from each host verified both arms against
gateway joint readback and reported collision state `INIT`.

The idle Mac service was gracefully restarted; the inactive Pi/Lynx services
were restarted with the same configuration. Mac remains selected, and all three
services answer. Placement and recovery files were preserved. No scan, pick,
gripper action, Home, Stop, reset or record release was sent for this correction.
The gateway was not restarted.

The main panel now exposes Home, grippers and status refresh outside collapsed
settings. Advertised but blocked controls show their reason; unavailable Reset
routes remain hidden. **Pick selected cup** stays visible and explains the need
for a fresh scan and selection. It still opens the existing single-cup preview
and requires confirmation. Frontend files are served by the Mac dashboard;
refresh the browser to load the revised layout. Managed service startup also
discovers support consistently, even when no optional controller profile exists.

## Reopen the Mac dashboard

The services are already running. This launcher reuses the local live service
and dashboard when present; otherwise it starts them. It does not start a robot
job or restart remote devices.

```sh
cd "/Users/devkasunx/Documents/MDU/Robotics and Advanced Embedded Systems Project Course/codex/rapid/Fail-Safe-Edge-AI-Picker-HT26"
/Users/devkasunx/miniforge3/bin/python ../../tmp/device-dashboard-20261113/start_live_dashboard.py
```

Then open **http://localhost:8086/**. Private configuration, service tokens, logs
and ownership records are under `codex/tmp/device-dashboard-20261113/private`.
Keep this installation directory: it contains persistent state, not disposable
test data. Credentials are excluded from Git and the Lynx package.

## After restarting a remote device

Pi and Lynx services currently run detached from SSH. They have not been added
to OS boot startup. Use these only when that device's picker service is stopped.
The same-state lock rejects a duplicate controller.

On the Pi:

```sh
cd /home/kasun/picker-devices-20261113
nohup ./start-service.sh >>service-live.log 2>&1 </dev/null &
```

On Lynx, start the existing `cuppicker` application through management if needed,
then attach to its shell:

```sh
nohup sh /state/cli-0.2.0/start-device-service.sh >>/state/cli-0.2.0/device-service-live.log 2>&1 </dev/null &
```

Keep the separate gateway running. Do not run the old interactive picker while
the managed service holds its state lock. A model startup failure requires
closing/restarting that service; no movement or model operation is automatically
retried. If the network addresses change, update the private device configuration
and the service launchers while idle.

## What was verified

- Real camera scans and image delivery on all three devices in dry-run mode.
  Each scan used one model observation and five raw depth samples. The retained
  verification policy is one pass with a 60-second age limit.
- Dry-run switching **Lynx → Pi → MacBook → Lynx**, including unloading and
  restarting the native model. Only the selected service held an active picker.
- A complete MacBook pick/handover/place/home proposal was prepared without
  confirming it. No physical picking was performed in this deployment check.
- Idle live-mode switching **Lynx → Pi → MacBook → Lynx**, using the gateway's
  explicit idle/no-lease check. The final selected device was Lynx, with no stale
  scan or proposal retained.
- P5's old reservation was released after the user's current empty-position
  inspection. P2 remained occupied. Seven placement coordinates, calibration,
  table reference and recovery state were preserved across scans and handoffs.
  The original Lynx files were backed up under
  `/state/cli-0.2.0/backups/before-device-dashboard-20261113T125706Z`.
- The initial backend validation passed 116 tests plus 138 subtests. Subsequent
  HTTP responsiveness/status fixes passed 52 affected tests plus 79 subtests;
  the three real loopback HTTP tests passed separately. Dashboard JavaScript:
  47 passing tests, with desktop/mobile browser checks and the actual connected
  dashboard inspected.

Lynx package **0.2.6** is 45.3 MB and contains no NumPy, OpenCV Python, PyTorch,
Ultralytics, FastAPI or Uvicorn runtime dependency. Package SHA-256:
`f0e84d10ea62d44b0c4179b03b88145476a29a64def400c12ba2e633115a9639`.
The exact package is in `codex/tmp/device-dashboard-20261113`.

The startup check reported **133.0 MiB** shared headroom against the unchanged
128 MiB guard. During one scan with dashboard polling/image delivery, 55 one-second
samples reached **203,386,880 bytes** of shared usage against the
**254,283,776-byte** limit; the picker cgroup peak was **162,152,448 bytes**.
The shared historical peak already equalled the limit and was not reset, so it
cannot identify this run's peak. Counters remained `max=827`, `oom=0`,
`oom_kill=0`; no new limit/OOM event occurred. Sampling is not a guarantee for
every later workload.

Natural-language/Ollama matching is disabled in managed-device mode. Cup
selection, terminal commands, picking, status, and supported robot controls use
the selected device's existing controller and confirmation workflow.
