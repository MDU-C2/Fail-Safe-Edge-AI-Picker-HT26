# Gateway diagnostics

`gateway_diag.py` is a read-only terminal tool for the Robot Gateway. It
acts as the `diagnostics` client (`robot.read` + `camera.read`, priority
10), so it can show everything and change nothing: it never takes the
control lease, moves the robot or uploads anything. It works against
either implementation (Python or C++).

```powershell
pip install -r diagnostics/requirements.txt     # requests, websockets, rich

python diagnostics/gateway_diag.py                          # = watch, local gateway
python diagnostics/gateway_diag.py --target pi watch        # the Raspberry Pi
python diagnostics/gateway_diag.py --target lynx status     # the Westermo Lynx
python diagnostics/gateway_diag.py --url http://host:8000/api/v1 status --json
python diagnostics/gateway_diag.py events --events control robot.rapid
python diagnostics/gateway_diag.py get robot/arms/left/joints
```

Run it from the repository root, or anywhere else with the full path.
In Git Bash, write `get` paths without the leading slash (`robot/arms`),
because MSYS turns `/robot/...` into a Windows path.

## Commands

| Command | What it does |
|---|---|
| `watch` (default) | Full-screen dashboard, polled every `--interval` s (default 2): health, camera (frame age, fps, sizes, intrinsics), robot controller (motors, mode, RAPID, per-arm RWS job status, legacy socket), arm poses (x y z, rx ry rz as in `OrientZYX`, configuration, gripper opening), control lease holder and expiry, stored calibration, RAPID tasks and modules, and the live event log. An event brings the next poll forward. Ctrl+C quits. |
| `status` | One snapshot of the same panels. Exit code 0 = `ok`, 1 = `degraded`, 2 = gateway unreachable, so it can be used in scripts. `--json` prints the raw results. |
| `events` | Streams events (`--events` names or prefixes, default all families; `--json` for one raw message per line). |
| `get PATH [key=value ...]` | One GET below the API base with the diagnostics identity, printing the JSON. |

Global options: `--url` (or `GATEWAY_URL`), `--target local|pi|lynx`,
`--token`, `--client` (development mode, default `diagnostics`),
`--timeout`, `--insecure` (skip TLS verification).

## Identity

- **strict mode**: `Authorization: Bearer <token>`. The token is taken
  from `--token`, then `GATEWAY_DIAG_TOKEN`, then the `diagnostics`
  entry of `client_tokens.json` at the repository root (local only,
  git-ignored). A rejected token shows as `AUTH REJECTED` in the header.
- **development mode**: `X-Dev-Client: diagnostics` (`--client`), and
  `?client=diagnostics` on the WebSocket.

Both headers are sent on every request; the gateway uses the one its
mode needs.

## Events

The events WebSocket (`/api/v1/events`) uses the same identity as the
HTTP requests: in **strict** mode the Bearer token goes in the upgrade
request's `Authorization` header (gateway 0.4.0 and later, both
implementations), in **development** mode `?client=<--client>`. In strict
mode without a token `watch` only polls and `events` exits with code 2;
a rejected token shows as `refused` in the events panel (exit code 2 for
`events`). So `--target lynx watch` shows the Lynx gateway's events live:

```powershell
python diagnostics/gateway_diag.py --target lynx watch
python diagnostics/gateway_diag.py --target lynx events --events control robot.status
```

## What it polls

`/health` (no auth) first. If the gateway answers, `/control`,
`/camera/status`, `/robot/status` and `/calibration` follow in parallel.
`/robot/rapid`, `/robot/arms`, `/robot/arms/{arm}/gripper` and
`/robot/rws/status?arm=` follow only while `/health` reports the robot
controller reachable, so a controller that is off the network costs
one fast probe per poll instead of a timeout per endpoint.
`/camera/intrinsics` is fetched once. The lease expiry is shown against
the gateway's clock (from the HTTP `Date` header), so a Pi or Lynx with
a wrong clock still shows the right countdown, to within a second.

Tests: `python -m pytest gateway/tests/test_diagnostics_cli.py`.
