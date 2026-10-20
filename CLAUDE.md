# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MDU robotics project course (DVA490/DVA474, HT2026): a fail-safe dish-picking system where cloud / Raspberry Pi / Lynx AI models degrade in that order down to a safe stop, and all of them drive an ABB YuMi through one trusted **Robot Gateway**. Nearly all code in this repo is that gateway ([gateway/](gateway/)), a FastAPI service fronting the robot and an OAK-D Pro W PoE RGB-D camera.

Other contents:
- [send_coordinate.py](send_coordinate.py) – standalone script that sends `{"x","y","z"}` (mm) as a JSON line over a raw TCP socket to the YuMi RAPID socket server (`192.168.1.10:5000`). It is not wired into the gateway yet.
- [rapid/](rapid/) – the RAPID program for the YuMi, shared by the team and the gateway (see "RAPID program" below): the arm modules, the team's `CommServer` socket server, and `sim/GripperStub.mod` for RobotStudio. Used by [calibration/](calibration/), [capture_and_detect/](capture_and_detect/) and the gateway's RWS API. Other people work on it; do not change it unless asked.
- [threat_analysis/](threat_analysis/) – OWASP Threat Dragon STRIDE model. `threat_model.json` is the source of truth; the PDF is an export and must not be hand-edited.

## Commands

All commands run from the **repository root**. `Settings` loads `gateway/.env` and `gateway/auth_clients.json` via CWD-relative paths, and the code imports itself as the `gateway.*` package, so running from inside `gateway/` breaks both.

```powershell
# Setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r gateway/requirements.txt
Copy-Item gateway/.env.example gateway/.env

# Run the gateway (docs at http://127.0.0.1:8000/docs)
uvicorn gateway.main:app --host 127.0.0.1 --port 8000 --reload

# Plain Docker image (build context must be the repo root)
docker build -f gateway/docker/DockerFile -t gateway .
```

Without the hardware on the network, set `GATEWAY_CAMERA_PROVIDER=mock` and `GATEWAY_ROBOT_PROVIDER=mock` in `gateway/.env` (as `.env.example` does). The code defaults are the real devices (`oak`, `tcp`) and `strict` security; with `oak`, startup blocks retrying the camera connection for up to 60 s before the app fails to start. Once it has started, a lost camera is reconnected in the background (see Camera).

### Tests

The pytest suite is in [gateway/tests/](gateway/tests/); configuration and markers are in [pytest.ini](pytest.ini). Install the extra dependencies with `pip install -r gateway/requirements-dev.txt`.

```powershell
python -m pytest                          # mock camera + mock robot, no hardware (~50 s)
python -m pytest --robotstudio            # also the RobotStudio virtual controller
python -m pytest --oak                    # also the real OAK camera
python -m pytest gateway/tests/test_real_robot.py gateway/tests/test_real_robot_legacy.py --real-robot   # MOVES THE REAL YUMI (small moves, see below)
python -m pytest gateway/tests/test_control.py -k preempt
```

- The suite starts its own gateways as `uvicorn` subprocesses on free ports ([gateway/tests/support.py](gateway/tests/support.py)), so no gateway needs to be running and port 8000 is not used. Each gets a temporary `auth_clients.json` with known tokens (`test-token-<client>`) and a temporary calibration file, so `gateway/auth_clients.json` and `gateway/calibration.json` are never touched. Settings go in as `GATEWAY_*` environment variables, which override `gateway/.env`; the lease is shortened to 2 s.
- Session fixtures in [conftest.py](gateway/tests/conftest.py): `strict_gateway`, `development_gateway`, `gateway` (parametrised over both modes), `robotstudio_gateway`, `oak_gateway`. `Client.control()` holds the lease with a renewal thread; an autouse fixture releases leftover leases after every test.
- Mock gateways point RWS at a closed local port, so RWS endpoints give 503 there; `test_rws.py` covers what the gateway itself enforces.
- `--robotstudio` (`test_robotstudio.py`) needs RobotStudio with the virtual controller on `127.0.0.1:80`. It loads/unloads a `GatewayTest` module, starts/stops RAPID, and end to end loads the arm modules from `rapid/` and moves both virtual arms and the gripper through `POST /robot/rws/move|home|gripper` (pose, orientation, unreachable, gateway safe zone, busy-arm and not-running 409s, arms independent, `wait: false` + `GET /robot/rws/status` with both arms at once), then loads the left arm with the team's [rapid/CommServer.mod](rapid/CommServer.mod) in `T_ROB_R` (the virtual controller has no `communication` task; `SERVER_IP` is replaced by 127.0.0.1, port 5001) and moves through the legacy `POST /robot/command` with `STARTED`/`STATUS` polling. The program modules present before are saved over RWS and loaded back afterwards; RAPID is left stopped.
- `--real-robot` (`test_real_robot.py`) **moves the real YuMi** through an already running gateway (`--real-robot-url`, default the Pi; `--real-robot-token`, default `BAD_TOKEN_OPERATOR`) and the RAPID program already running there – nothing is loaded, started or stopped, and it fails before any move unless the robot is in AUTO, motors on, RAPID running and both arms idle. Moves are only straight up from the current pose and back, 30 mm (90 mm for the busy-arm test and for both arms at once, which also checks both report BUSY together) (each test returns the arm to its start, also on failure): wait and `wait: false` + `/status`, the busy-arm 409, both arms at once; refusals (safe zone, out of reach, validation) and the legacy `/robot/command` `STATUS` move nothing. `--real-robot-gripper` adds closing and opening both grippers. `test_real_robot_legacy.py` (same flag and rules) does the same through the legacy `/robot/command` and the team's CommServer (move up and back per arm, both arms at once, `ERR busy`, `ERR bad format`, CommServer's safe zone, out of reach) and checks the two paths side by side: an RWS job makes CommServer answer `ERR busy` and a CommServer job makes RWS answer 409, both status queries see either job, and RWS on one arm runs with legacy on the other. Shared fixtures and helpers are in [gateway/tests/real_robot.py](gateway/tests/real_robot.py). All their clients share one token, i.e. one identity, so it cannot test the lease. Only run it when the user asks.
- Without `--real-robot` every robot address in the tests is `127.0.0.1`; the real YuMi is never contacted. `--oak` needs `GATEWAY_CAMERA_IP` from `gateway/.env` and no other process holding the camera.

When changing gateway behaviour, add or update tests and run the suite (plus `--robotstudio` for RWS changes) before reporting it done.

Client tools in [gateway/scripts/](gateway/scripts/) take `--url`, `--token` (strict) and `--client` (development):

| Script | Purpose |
|---|---|
| `client_template.py` | Template client: control + lease renewal + camera to NumPy + `run_model()` stub returning RWS jobs (`move`/`home`/`gripper`, `wait: false` + `/status`, both arms at once); its docstring is the client-side reference for the RWS job API (bodies, replies, errors); releases control on exit |
| `basic_client.py` | The template on the RWS API: loads + starts the arm modules from `rapid/` (`--no-load` skips), then sends `run_model()`'s moves to `/robot/rws/move|home` (default: a demo path for the first frame); moves whatever `GATEWAY_ROBOT_HOST` points at |
| `rws_move.py` | One RWS job for one arm (`--x --y --z --rx --ry --rz`, each optional: missing values come from the current tool0 pose, so `--rz 90` turns in place and `--z 300` only changes height; `--home`, `--grip`/`--open`, `--arm right`, `--no-wait` polls `/robot/rws/status`, `--speed` mm/s / `--acc` / `--ramp` % for moves and home); prints the pose (with rx/ry/rz) before and after. Defaults to the Pi gateway, i.e. the real YuMi |
| `capture_images.py --count N` | Saves RGB / 16-bit depth / depth visualisation (`numpy`, `opencv-python`) |
| `save_rgb.py` | Writes the latest RGB frame to `rgb.png` (Pillow) |
| `watch_events.py` | Subscribes to the events WebSocket and prints events (development mode only) |
| `legacy_socket_client.py` | Sends `x,y,z[,rx,ry,rz]` / `HOME` / a pose list straight to the team's CommServer (`--right` for the right arm), polling `STATUS` until done, bypassing the gateway (default `localhost:5001`) |
| `generate_token.py <client>` | Random token plus its `auth_clients.json` entry |

Pillow, OpenCV and NumPy are script-only dependencies and are deliberately absent from `gateway/requirements.txt` (the gateway itself passes raw DepthAI buffers through untouched).

### Raspberry Pi deployment

In the lab the gateway runs as Docker container `robot-gateway` (`--network host`) on a Raspberry Pi 5 (`169.254.125.202` / `192.168.125.201`, user `kasun`, no internet). The image is built on a PC for `linux/arm64`, saved to `gateway/docker/images/` (git-ignored), copied with `scp` and loaded with `docker load`. The full steps are in [gateway/README_START.md](gateway/README_START.md) under "Deploy to the Raspberry Pi". Only one process can hold the OAK camera, so a gateway running elsewhere must be stopped first.

### WeOS container package

[gateway/docker/generate-container.py](gateway/docker/generate-container.py) is a Westermo-supplied utility that turns the Dockerfile into a signed `.wmopkg` for Westermo WeOS container platforms (Docker buildx OCI export → rootfs → squashfs → FIT image):

```bash
python gateway/docker/generate-container.py -i gateway/docker/DockerFile -t docker \
    -n gateway -v 1.0 -d "<description>" -a <platform, e.g. linux/arm64> [-k <signing key>]
```

It shells out to `docker buildx`, `mksquashfs`, `mkimage` (U-Boot tools) and `file`, so it needs Linux/WSL. It writes a `tmp_<uuid>/` working directory and `<name>-<version>.wmopkg` into the CWD.

## Gateway architecture

[gateway/README.md](gateway/README.md) is the original design document. It describes intent (and an older `server:app` layout), not the current state – check the code before relying on it. [gateway/README_START.md](gateway/README_START.md) is the accurate quick start.

### Request path

Every protected route depends on `require_permission("<perm>")` from [gateway/security/authentication.py](gateway/security/authentication.py), which resolves an `Identity` (client id, permission set, control priority) via `get_identity`. The security *mode* only changes how the identity is produced; the permission checks downstream are identical:

- `development` – identity comes from the hard-coded `DEVELOPMENT_IDENTITIES` (`operator` 100, `autonomy` 50, `diagnostics` 10, read-only), selected with the `X-Dev-Client` header (default `operator`). On the WebSocket it is the `?client=` query parameter instead.
- `strict` – `Authorization: Bearer <token>`; the SHA-256 of the token is looked up in `auth_clients.json` by `ClientRegistry`. The registry is loaded and validated once at startup (`get_client_registry()`, cached), so a missing or malformed file stops the gateway from starting and edits need a restart.

Permissions in use: `robot.read`, `robot.control`, `camera.read`, `camera.control` (`*` is a wildcard).

Routes that move the robot or change its program or calibration use `require_control("robot.control")` from [gateway/control/router.py](gateway/control/router.py) instead: the permission (403) **and** the control lease (409). Use it for any new such route rather than checking the lease by hand.

### Health

`GET /health` ([gateway/health.py](gateway/health.py), no authentication, always 200 while the gateway runs) reports `status` `ok` or `degraded`, version, uptime, security mode, robot provider, whether a control lease is active, the camera (connected, newest frame age, error) and the robot controller over RWS (reachable, motor state, operation mode, RAPID execution state, whether a gateway move is in progress, error). The RWS probe uses a 0.5 s connect timeout so the endpoint stays fast when the controller is off the network; the test harness polls it to detect startup. It shows no client ids or tokens.

### Control lease

[gateway/control/manager.py](gateway/control/manager.py) holds a single in-memory lease covering both robot and camera control. A strictly higher `control_priority` preempts; equal priority does not. Expiry (`GATEWAY_CONTROL_LEASE_SECONDS`, default 5) is evaluated lazily whenever the lease is next read – there is no background timer, so no event fires and no safe-state action is taken when a lease lapses. Routes: `GET /control`, `POST /control/acquire`, `POST /control/renew` and `POST /control/release` (owner only, publishes `control.released`).

The manager instance is a module global in [gateway/control/router.py](gateway/control/router.py), created by `configure_control_manager()` from `main.py`; other routers reach it through `get_control_manager()`.

### Camera

Producer/consumer split around a module-level singleton, `camera_service` ([gateway/camera/service.py](gateway/camera/service.py)), which keeps only the latest RGB+depth frame set. Reads never consume, so any number of clients can poll. A `CameraProducer` (selected in [gateway/camera/factory.py](gateway/camera/factory.py), started and stopped by the FastAPI lifespan in `main.py`) pushes into it. The mock producer publishes a 640x480 test pattern once a second in the same wire format as the OAK. `oak_producer` is imported lazily so `depthai` is only needed when the provider is `oak`; its blocking DepthAI calls go through `asyncio.to_thread`.

Wire format of `/camera/rgb` and `/camera/depth` is raw bytes with metadata in `X-Frame-Width`, `X-Frame-Height`, `X-Pixel-Format`, `X-Frame-Sequence`, `X-Frame-Timestamp` headers:
- RGB: `BGR888p` – **planar** (all B, then all G, then all R), `3 * W * H` bytes.
- Depth: `RAW16` – uint16 millimetres, 0 = invalid, aligned to the RGB camera so `depth[v, u]` matches `rgb[v, u]`.

The OAK producer takes its IP, FPS and RGB size from settings (`GATEWAY_CAMERA_IP`, `GATEWAY_CAMERA_FPS`, `GATEWAY_CAMERA_RGB_WIDTH/HEIGHT`). `/camera/rgb` and `/camera/depth` answer 503 instead of serving a frame when the camera is disconnected or the newest frame is older than `GATEWAY_CAMERA_MAX_FRAME_AGE_SECONDS` (default 5), so clients never act on a stale scene. When capture fails, or no frame arrives for 10 s, the OAK producer closes the device and reconnects in the background: 5 attempts 5 s apart, then one every 60 s until it is back (constructor arguments of `OakCameraProducer`); meanwhile `/camera/status` and `/health` report it disconnected with the reason in `error`. `test_oak_reconnect.py` tests this with a fake device. `GET /camera/intrinsics` returns the CAM_A intrinsics read from the device calibration, converted to that same size.

### Calibration

`POST /calibration` stores an arbitrary JSON object (max 1 MB) and requires `robot.control` **and** the control lease, like robot commands; `GET /calibration` needs `camera.read` and returns `{calibration, updated_at, updated_by}` (404 until something is uploaded). `calibration_service` ([gateway/calibration/service.py](gateway/calibration/service.py)) keeps it in memory and writes it atomically to `GATEWAY_CALIBRATION_FILE` (default `gateway/calibration.json`, git-ignored), loading it again at startup; a corrupt file stops startup. In Docker that file lives inside the container, so it survives restarts but not a redeploy. Uploads publish a `calibration.updated` event (subscribe to `calibration`).

### Events

`event_manager` ([gateway/events/manager.py](gateway/events/manager.py)) is another module singleton, published to by the control router, the robot service and the camera service. Clients connect to `WS /api/v1/events` and send `{"action": "subscribe" | "unsubscribe", "events": [...]}`.

- Subscribable names are exactly the keys of `EVENT_PERMISSIONS` in [gateway/events/permissions.py](gateway/events/permissions.py); a new event family must be added there or subscriptions to it are rejected as `unknown_event`. One bad name rejects the whole request.
- Delivery matches by dotted prefix, so subscribing to `control` receives `control.acquired` and `control.renewed`.
- `control.revoked` is sent straight to the preempted client regardless of its subscriptions.
- Bad messages (invalid JSON, not an object, bad `events`, unknown action) get a `{"type": "error"}` reply and the connection stays open; it is unregistered from `event_manager` whenever the handler ends.
- The WebSocket only works in `development` mode; in `strict` mode the connection is closed with 1008 because token auth is not implemented for it.

### Robot

[gateway/robot/service.py](gateway/robot/service.py) holds two implementations, chosen by `GATEWAY_ROBOT_PROVIDER` in `create_robot_service()`: `mock` (`MockRobotService`, tracks an `idle`/`running` state, contacts no hardware) and `tcp` (`RobotService`, sends the raw `command` text to the robot TCP server at `GATEWAY_ROBOT_HOST:GATEWAY_ROBOT_PORT` and returns its reply unchanged as `message`). The team's `rapid/CommServer.mod` (task `communication` on the real YuMi, port 5001) takes one message per connection: `x,y,z[,rx,ry,rz]` / `HOME` / `GRIP` / `OPEN` answer `STARTED` at once, `STATUS` answers `BUSY` / `DONE` / `ERR move failed`, an `R,` prefix addresses the right arm, refusals are `ERR <reason>` (`ERR busy`, `ERR bad format`, `ERR outside safe zone`). The client polls `STATUS` itself (see `rapid/tests/handover.py`), so both arms can run at once; the gateway does not wait for moves. `accepted` is true for `STARTED`/`BUSY`/`DONE` (the old fixture's synchronous `DONE` still counts) and false for `ERR ...` or a connection error; the `robot.status` event state is `command_started`/`busy`/`command_completed`/`command_failed`. `test_robot_tcp.py` checks this against a fake CommServer. `RobotCommand` is a free-form `command` string plus a `parameters` dict with no per-command schema or safety validation yet. With `tcp`, `POST /robot/command` moves the real robot – never send it as a test without the user asking.

`POST /robot/command` is **deprecated** in favour of `POST /robot/rws/move|home` but keeps working unchanged: it is flagged `deprecated` in OpenAPI, its responses carry `Deprecation: @<timestamp>` and `Link: <rws/move>; rel="successor-version", ...` headers, and the first call per process logs a warning. Do not remove it or change its behaviour; existing clients depend on it.

### Robot Web Services (arm pose, RAPID control, moves)

[gateway/rws/](gateway/rws/) talks to the controller over RWS 1.0 (RobotWare 6.16; RobotWare 7 has a different API) at `GATEWAY_ROBOT_HOST:GATEWAY_RWS_PORT` (the same host as the legacy socket server) with digest auth (`GATEWAY_RWS_USER` / `GATEWAY_RWS_PASSWORD`, default `Default User` / `robotics`). There is no mock: the default host is the real YuMi, so set `GATEWAY_ROBOT_HOST=127.0.0.1` for the RobotStudio virtual controller. `rws_client` ([gateway/rws/client.py](gateway/rws/client.py)) is a blocking `requests` session behind an `RLock`, called through `asyncio.to_thread`; mastership is taken and released per load/unload/reset. Controller errors are mapped in [gateway/rws/errors.py](gateway/rws/errors.py): unreachable 503, controller 400/404 passed on, 403/409 → 409, else 502.

- `GET /robot/arms` (both arms), `GET /robot/arms/{left|right}/pose?tool=&wobj=&coordinate=Base|Wobj|Tool`, `GET /robot/arms/{left|right}/joints` – `robot.read`. Pose fields are the robtarget (`x y z` mm, `q1..q4`, `cf1 cf4 cf6 cfx`, `arm_angle`); without `tool`/`wobj` the arm's active ones are used and reported.
- `GET /robot/rapid` – `robot.read`; execution state, motor/op mode and the program modules in each task.
- `PUT /robot/rapid/tasks/{task}/modules/{module}` (`{"source": "..."}`, must declare `MODULE <module>`, ISO-8859-1, replaces a same-named module), `DELETE` the same path, `POST /robot/rapid/start` (`{"reset_program_pointer": bool}`), `POST /robot/rapid/stop` – `robot.control` **and** the control lease. Load/unload return 404 for an unknown task and 409 while RAPID runs. They publish `robot.rapid.loaded|unloaded|started|stopped` (subscribe to `robot.rapid`).
- `POST /robot/rws/move` (`{"arm"?, "x","y","z"}` mm, optional `"rx","ry","rz"` degrees, all three or none, same meaning as RAPID `OrientZYX(rz, ry, rx)`), `POST /robot/rws/home` (body optional, `{"arm"?}`); both take optional `"speed"` (TCP mm/s, 1–1500, default `GATEWAY_RWS_DEFAULT_SPEED` 50 = v50) and `"acceleration"` (20–100 %) / `"ramp"` (10–100 %) (RAPID `AccSet`, default 100), written with every job into `move_speed_<arm>` (speeddata `[speed,500,5000,1000]`) / `move_acc_<arm>` / `move_ramp_<arm>`, so nothing carries over between RWS jobs (legacy CommServer moves use whatever was written last); a program without these variables gives 409 before anything is written and `POST /robot/rws/gripper` (`{"arm"?, "action": "grip"|"open"}`) – `robot.control` **and** the lease; `arm` is `left` (default) or `right`. They wait for the job by default; with `"wait": false` they answer `STARTED` once it is written, and `GET /robot/rws/status?arm=` (`robot.read`, no lease) answers like CommServer's `STATUS`: `BUSY` while the arm works (also on a job from another client), then `DONE` or `ERR move failed`. Together with the per-arm 409 (CommServer's `ERR busy`) and the gateway safe zone this covers everything CommServer offers, so clients like `rapid/tests/handover.py` can drive both arms at once over RWS. `rws_motion` ([gateway/rws/motion.py](gateway/rws/motion.py)) writes the per-arm PERS variables of `rapid/SharedVars.mod` (`target_pos_left`, `go_home_right`, `grip_cmd_left`, ...) in the arm's task (`T_ROB_L` / `T_ROB_R`; paths `/rw/rapid/symbol/data/RAPID/<task>/<var>`, no mastership – the controller refuses it while RAPID runs), sets `move_request_<arm>`, polls it every 50 ms until RAPID clears it, then reads `move_ok_<arm>`. Returns the legacy `CommandResponse` (`DONE`, or `accepted: false` with `ERR move failed`). The arm tasks check no safe zone (the team's CommServer did), so the gateway refuses targets outside `GATEWAY_RWS_SAFE_ZONE_MIN/MAX` (default ±1000 mm, CommServer's limits) with `ERR outside safe zone` before writing anything. 409 if RAPID is not running, the variables are missing, or that arm is busy (one job per arm, also after a timeout until RAPID finishes, or while CommServer runs a job on it; the two arms are independent); 504 after `GATEWAY_RWS_MOVE_TIMEOUT` (60 s). Publishes `robot.status` like the tcp provider. With the real `GATEWAY_ROBOT_HOST` these move the real robot – never call them as a test without the user asking.

### RAPID program

The robot-side program is the team's [rapid/](rapid/) folder; there is no separate gateway copy. The gateway drives its arm modules over RWS and does not need `CommServer.mod` (the team's socket server, which keeps running in the `communication` task on the real YuMi for `/robot/command`). `SharedVars.mod` declares the per-arm command PERS variables (`move_request_<arm>`, `go_home_<arm>`, `move_ok_<arm>`, `use_rot_<arm>`, `target_pos_<arm>`, `target_rot_<arm>`, `grip_cmd_<arm>`, and the speed `move_speed_<arm>` (speeddata, declared v50) and `move_acc_<arm>`/`move_ramp_<arm>` (`AccSet` %, declared 100), which the arm loop applies with `AccSet` before each job and both the move and the home routines use) and is loaded into both tasks; `LeftArmInstructions.mod` (module `LeftArm`) + `GoHomeLeft.mod` (`HomeLeft`) run in `T_ROB_L`, the right-arm pair in `T_ROB_R`. Each arm waits for `move_request_<arm>`, then homes (`go_home`), grips/opens (`grip_cmd`, SmartGripper `g_GripIn`/`g_GripOut`) or moves tool0 in wobj0 if `CalcJointT` finds it reachable, and reports `move_ok`. Changes to these variables or to the arm loop must be matched in [gateway/rws/motion.py](gateway/rws/motion.py). The arm modules call the YuMi SmartGripper routines (`g_Init`, `g_IsCalibrated`, `g_GripIn`, `g_GripOut`), which the real YuMi has in its `YuMiLib*` system modules but the RobotStudio virtual controller (created without the SmartGripper option) lacks, so RAPID will not start there; [rapid/sim/GripperStub.mod](rapid/sim/GripperStub.mod) defines no-op versions and is loaded next to the arm modules in simulation only (`--gripper-stub` in `rapid/rapidctl.py` and `basic_client.py`, always in `test_robotstudio.py`). Never load it on the real robot. Keep RAPID changes minimal: other people work on the RAPID code.

[rapid/rapidctl.py](rapid/rapidctl.py) manages the program on a controller over RWS (requests only, no gateway needed); it defaults to the **real** YuMi (`--host 127.0.0.1` for RobotStudio):

```powershell
python rapid/rapidctl.py status                 # mode, motors, RAPID state, modules per task
python rapid/rapidctl.py upload [--task T_ROB_L] [--no-start]   # back up, stop, load rapid/'s files, pointer to main, start
python rapid/rapidctl.py start [--main] | stop | restart
python rapid/rapidctl.py reset [--no-start]     # stop, SharedVars' PERS back to their declared values, pointer to main, start
python rapid/rapidctl.py speed [--arm left] [--set MM_S] [--acc %] [--ramp %]   # show / set move_speed/acc/ramp (while running, no mastership)
python rapid/rapidctl.py delete --task T_ROB_L [MODULE ...]     # back up, stop, unload
python rapid/rapidctl.py backup | restore rapid/backups/<dir>
python rapid/rapidctl.py --host 127.0.0.1 upload --gripper-stub
```

`upload` loads all three tasks as the `.pgf` files list them (`T_ROB_L`, `T_ROB_R`, and `communication` with CommServer; a task the controller lacks, like `communication` in RobotStudio, is skipped). Changing commands save the modules they replace to `rapid/backups/<host>-<time>/` (git-ignored), ask before changing a controller other than 127.0.0.1 (`-y` skips), and need AUTO: in manual mode the controller refuses remote program changes and starts (the script says so). `--gripper-stub` is refused for the real robot. `start` checks that RAPID really runs (a program with errors is accepted but does not start). PERS variables survive stop, restart and even `upload` (a PERS lives on while any task declares it), so a half-done job (`move_request_<arm>` still TRUE) comes back after a restart; `reset` writes every PERS of `SharedVars.mod` back to the value declared in the file (parsed from it, once, in the first task that has `SharedVars` – they are shared) and reads them back. It replaced `gateway/rapid/upload_rapid.py`.

## Things to be aware of

- `gateway/.env` is git-ignored and no longer tracked, so each machine has its own (copy `gateway/.env.example`); `gateway/auth_clients.json` is still tracked, so edits to it show up as changes.
- There is no `.dockerignore` (it was removed because it caused problems with the Westermo build), so `COPY gateway ./gateway` puts `gateway/.env` and `gateway/auth_clients.json` into the image as they are at build time.
- Configuration is `GATEWAY_`-prefixed env vars / `gateway/.env`, defined in [gateway/config.py](gateway/config.py) and cached by `get_settings()` (`lru_cache`), so changes require a restart.
- The code style is heavily vertical (one argument per line, `# ====` section banners); match it when editing gateway files.
