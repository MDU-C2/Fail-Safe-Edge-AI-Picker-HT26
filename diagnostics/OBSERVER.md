# Browser observer

The **Observer** tab presents the read-only readings used by `gateway_diag.py`
inside the picker dashboard. The same component runs independently on a Windows
laptop, without starting a picker, loading YOLO or taking a control lease.

It shows gateway/controller health, the current lease holder, the accepted
queue's original owner and completed steps, both arm poses and gripper openings,
camera status, calibration metadata, RAPID tasks and a history of observed changes.
Queue ownership and lease ownership are deliberately separate during takeover.
The primary role is labelled **Primary computer**, since it may be a Mac, Windows
PC or another primary worker.

## Windows: the full UI alongside the Mac

Use this when Windows should show **all dashboard tabs**, including Observer and
3D, while the Mac remains the primary controller. Connect Windows to the robot's
Ethernet network. After pushing the new files and pulling that same branch on
Windows, run these commands from the repository root in PowerShell:

```powershell
git pull --ff-only
py -m pip install -r capture_and_detect/requirements-monitor.txt
py capture_and_detect/web_app.py --monitor --gateway http://192.168.125.20:8000 --port 8087
```

Open **http://localhost:8087 on Windows**, and keep its terminal open. The Mac can
keep running its usual dashboard on its own port 8087; localhost refers to the
computer where the browser is open. No Wi-Fi, Mac pairing or configuration/state
transfer is needed. Only one computer runs the controlling dashboard; the Windows
monitor runs alongside it and never joins the Mac → Pi → Lynx controller priority.

For a strict gateway, supply its existing diagnostics token (the same credential
as the smaller observer, described below):

```powershell
py capture_and_detect/web_app.py --monitor --gateway http://192.168.125.20:8000 --observer-token-file "$HOME\cup-observer\diagnostics-token" --port 8087
```

The private file must already contain that token. Do not put credentials in Git.
The monitor does not load `GATEWAY_TOKEN`, picker configuration, ownership records,
model weights or Assistant keys. Its dependencies are only the web server and an
image decoder; YOLO, PyTorch and OpenCV are not required.

- **Pick cups:** periodically displays the latest gateway RGB camera frame.
  Frames are unannotated; camera reads occur only while this tab is visible.
- **Robot controls:** displays gateway controller status; command buttons stay
  disabled. **3D view:** reads the robot's actual joints and grippers directly.
- **Observer:** gateway diagnostics, control handover, queue and timeline.
- **Assistant / Settings:** remain accessible, with editing and actions disabled.

Cup detections, placement occupancy, completed-cup batch counts, saved references
and Assistant conversations are not shared by the current failover workers. The
monitor explicitly marks those unavailable instead of presenting a separate
local picker's state. Queue progress describes the gateway's accepted queue, not
the entire batch. The monitor cannot issue Stop; use the controlling dashboard
or the robot's physical controls. All non-GET/HEAD requests are rejected by the
monitor server, even if a button or request is manually modified.

The Windows gateway connection keeps updating during a Mac Ethernet disconnect.
If Windows itself loses connectivity, it labels retained measurements and camera
frames as last known and resumes reads after reconnection.

For an offline full-UI layout preview:

```powershell
py capture_and_detect/web_app.py --monitor --demo --port 8087
```

This is explicitly simulated and has no real camera feed. On the Mac, keep using
the existing live launcher. Restart it after current robot work finishes to load
the new Observer routes. A server started before the change can load the new HTML
but return 404 for Observer CSS/JS; restarting the server, then refreshing the
browser, resolves that mismatch.

## Windows: the smaller Observer-only page

Connect Windows to the robot/Lynx Ethernet network and use a checkout containing
this change. From the repository root in PowerShell:

```powershell
py -m pip install -r capture_and_detect/requirements-web.txt
py diagnostics/observer.py --url http://192.168.125.20:8000 --port 8088
```

Open [the local observer](http://127.0.0.1:8088). Keep this terminal open. No Mac
pairing or Wi-Fi connection is required. The service listens only on Windows
localhost; its gateway reads use Windows' own Ethernet connection.

In strict gateway mode, provide the existing **diagnostics** credential using
`GATEWAY_DIAG_TOKEN`, the `diagnostics` entry of the local, ignored
`client_tokens.json`, or a private token file:

```powershell
py diagnostics/observer.py --url http://192.168.125.20:8000 --token-file "$HOME\cup-observer\diagnostics-token" --port 8088
```

That file must already contain the gateway's diagnostics token. This command
does not create credentials. Do not copy the primary picker's ownership records,
device configuration or control token to make a second picker. `robot.read` and
`camera.read` are the diagnostics permissions; the observer only sends fixed GET
requests. Development-mode gateways use `X-Dev-Client: diagnostics`.

The gateway address above is the current project's Lynx address. Substitute the
actual gateway address if your network differs. Ethernet addresses must be
unique and routable on that network. If access is rejected, check the diagnostics
token; if the gateway is unavailable, check the wired connection and address.

## Mac: the new tab in the existing dashboard

Once the robot is idle, restart the dashboard with its existing launcher and
open **Observer**. Existing picker settings and placement/ownership state are
preserved. The observer takes its gateway address from `--gateway` or the local
entry in `--device-config`, and its credential from `GATEWAY_DIAG_TOKEN` or the
local `client_tokens.json` diagnostics entry. Direct `web_app.py` launches also
accept `--observer-token-file /path/to/diagnostics-token`.

When no diagnostics credential is supplied, the main dashboard reuses its
existing gateway credential for these fixed GET requests, just as its existing
status reader does. This fallback is exclusive to the main dashboard. The
independent Windows observer never loads a primary picker credential or state.

**Open focused view** opens `/observer` with the same reader and authentication
as that dashboard server. This still depends on that server's network. For an
independent observer on the Mac, the Windows commands above also work with
`python` instead of `py`. For the unplug test, keep the independent service on
Windows; a Mac-hosted page cannot read through a disconnected Ethernet adapter.

## Offline preview

```powershell
py diagnostics/observer.py --demo --port 8088
```

The amber **Simulated demo** banner remains visible. Demo data is never used as
a fallback for a disconnected real gateway. The normal dashboard's `--demo`
mode supplies the same sample to its Observer tab.

## Reading behavior and limits

- One shared, sequential GET reader per server; opening more browser tabs does
  not create more gateway polling threads. Requests are paced, with a 12-second
  backoff on transport failures. The gateway's normal control path is unchanged.
- Browser polling runs only while Observer is visible. Gateway polling stops
  after a short grace period without viewers. Each reading carries its own age;
  unavailable and old readings are marked **Last known**. Reconnecting resumes
  reads automatically. A server restart starts a new observation history.
- The handover display reports who holds the lease. It does not infer that other
  workers are online. Unknown/rejected readings never mean the lease is free.
- The timeline records changes between polls, not the complete WebSocket event
  stream. Fast transitions can be missed. Queue progress is the current gateway
  queue, not the full multi-cup batch history.
- Camera **status** is included. The Pi's latest detection image, cup inventory
  and placement grid are not exported by this observer. It does not relay the
  worker-specific scene or replace the existing 3D view.
- The standalone server has no movement, Stop, recovery, upload or settings
  routes. The main dashboard retains its existing controls outside this tab.

## Offline checks

```sh
python -m unittest discover -s capture_and_detect -p test_gateway_observer.py
node --test capture_and_detect/test_observer_dashboard.js capture_and_detect/test_workspace_navigation.js
```

Tests use fake gateway responses and simulated HTTP requests; they send no robot
commands. A physical unplug/takeover trial remains a separate operator test.
