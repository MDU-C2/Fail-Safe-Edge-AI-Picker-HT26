# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MDU robotics project course (DVA490/DVA474, HT2026): a fail-safe dish-picking system where cloud / Raspberry Pi / Lynx AI models degrade in that order down to a safe stop, and all of them drive an ABB YuMi through one trusted **Robot Gateway**. Nearly all code in this repo is that gateway ([gateway/](gateway/)), a FastAPI service fronting the robot and an OAK-D Pro W PoE RGB-D camera.

Other contents:
- [send_coordinate.py](send_coordinate.py) – standalone script that sends `{"x","y","z"}` (mm) as a JSON line over a raw TCP socket to the YuMi RAPID socket server (`192.168.1.10:5000`). It is not wired into the gateway yet.
- [rapid/](rapid/) – the team's RAPID program (`CommServer` socket server, `LeftArmInstructions`, `GoHomeLeft`, `SharedVars`), used by [calibration/](calibration/) and [capture_and_detect/](capture_and_detect/). It is separate from [gateway/rapid/](gateway/rapid/) and has the same PERS interface; do not change it unless asked, other people work on it.
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
python -m pytest gateway/tests/test_control.py -k preempt
```

- The suite starts its own gateways as `uvicorn` subprocesses on free ports ([gateway/tests/support.py](gateway/tests/support.py)), so no gateway needs to be running and port 8000 is not used. Each gets a temporary `auth_clients.json` with known tokens (`test-token-<client>`) and a temporary calibration file, so `gateway/auth_clients.json` and `gateway/calibration.json` are never touched. Settings go in as `GATEWAY_*` environment variables, which override `gateway/.env`; the lease is shortened to 2 s.
- Session fixtures in [conftest.py](gateway/tests/conftest.py): `strict_gateway`, `development_gateway`, `gateway` (parametrised over both modes), `robotstudio_gateway`, `oak_gateway`. `Client.control()` holds the lease with a renewal thread; an autouse fixture releases leftover leases after every test.
- Mock gateways point RWS at a closed local port, so RWS endpoints give 503 there; `test_rws.py` covers what the gateway itself enforces.
- `--robotstudio` (`test_robotstudio.py`) needs RobotStudio with the virtual controller on `127.0.0.1:80`. It loads/unloads a `GatewayTest` module, starts/stops RAPID, and end to end loads the `gateway/rapid` program and moves the virtual left arm through `POST /robot/rws/move|home` (pose, orientation, safe zone, busy and not-running 409s), then loads it with the socket server fixture [gateway/tests/rapid/LegacyCommServer.mod](gateway/tests/rapid/LegacyCommServer.mod) and moves through the legacy `POST /robot/command`. The program modules present before are saved over RWS and loaded back afterwards; RAPID is left stopped.
- Every robot address in the tests is `127.0.0.1`; the real YuMi is never contacted. `--oak` needs `GATEWAY_CAMERA_IP` from `gateway/.env` and no other process holding the camera.

When changing gateway behaviour, add or update tests and run the suite (plus `--robotstudio` for RWS changes) before reporting it done.

Client tools in [gateway/scripts/](gateway/scripts/) take `--url`, `--token` (strict) and `--client` (development):

| Script | Purpose |
|---|---|
| `client_template.py` | Template client: control + lease renewal + camera to NumPy + `run_model()` stub + commands; releases control on exit |
| `basic_client.py` | The template on the RWS API: loads + starts the `gateway/rapid` program (`--no-load` skips), then sends `run_model()`'s moves to `/robot/rws/move|home` (default: a demo path for the first frame); moves whatever `GATEWAY_ROBOT_HOST` points at |
| `capture_images.py --count N` | Saves RGB / 16-bit depth / depth visualisation (`numpy`, `opencv-python`) |
| `save_rgb.py` | Writes the latest RGB frame to `rgb.png` (Pillow) |
| `watch_events.py` | Subscribes to the events WebSocket and prints events (development mode only) |
| `legacy_socket_client.py` | Sends `x,y,z[,rx,ry,rz]` / `HOME` / a pose list straight to the RAPID socket server, bypassing the gateway (default `localhost:5000`) |
| `generate_token.py <client>` | Random token plus its `auth_clients.json` entry |

Pillow, OpenCV and NumPy are script-only dependencies and are deliberately absent from `gateway/requirements.txt` (the gateway itself passes raw DepthAI buffers through untouched).

### Raspberry Pi deployment

In the lab the gateway runs as Docker container `robot-gateway` (`--network host`) on a Raspberry Pi 5 (`169.254.125.202` / `192.168.125.202`, user `kasun`, no internet). The image is built on a PC for `linux/arm64`, saved to `gateway/docker/images/` (git-ignored), copied with `scp` and loaded with `docker load`. The full steps are in [gateway/README_START.md](gateway/README_START.md) under "Deploy to the Raspberry Pi". Only one process can hold the OAK camera, so a gateway running elsewhere must be stopped first.

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

[gateway/robot/service.py](gateway/robot/service.py) holds two implementations, chosen by `GATEWAY_ROBOT_PROVIDER` in `create_robot_service()`: `mock` (`MockRobotService`, tracks an `idle`/`running` state, contacts no hardware) and `tcp` (`RobotService`, sends the raw `command` text to the robot TCP server at `GATEWAY_ROBOT_HOST:GATEWAY_ROBOT_PORT` and treats a `DONE` reply as accepted). `RobotCommand` is a free-form `command` string plus a `parameters` dict with no per-command schema or safety validation yet. With `tcp`, `POST /robot/command` moves the real robot – never send it as a test without the user asking.

`POST /robot/command` is **deprecated** in favour of `POST /robot/rws/move|home` but keeps working unchanged: it is flagged `deprecated` in OpenAPI, its responses carry `Deprecation: @<timestamp>` and `Link: <rws/move>; rel="successor-version", ...` headers, and the first call per process logs a warning. Do not remove it or change its behaviour; existing clients depend on it.

### Robot Web Services (arm pose, RAPID control, moves)

[gateway/rws/](gateway/rws/) talks to the controller over RWS 1.0 (RobotWare 6.16; RobotWare 7 has a different API) at `GATEWAY_ROBOT_HOST:GATEWAY_RWS_PORT` (the same host as the legacy socket server) with digest auth (`GATEWAY_RWS_USER` / `GATEWAY_RWS_PASSWORD`, default `Default User` / `robotics`). There is no mock: the default host is the real YuMi, so set `GATEWAY_ROBOT_HOST=127.0.0.1` for the RobotStudio virtual controller. `rws_client` ([gateway/rws/client.py](gateway/rws/client.py)) is a blocking `requests` session behind an `RLock`, called through `asyncio.to_thread`; mastership is taken and released per load/unload/reset. Controller errors are mapped in [gateway/rws/errors.py](gateway/rws/errors.py): unreachable 503, controller 400/404 passed on, 403/409 → 409, else 502.

- `GET /robot/arms` (both arms), `GET /robot/arms/{left|right}/pose?tool=&wobj=&coordinate=Base|Wobj|Tool`, `GET /robot/arms/{left|right}/joints` – `robot.read`. Pose fields are the robtarget (`x y z` mm, `q1..q4`, `cf1 cf4 cf6 cfx`, `arm_angle`); without `tool`/`wobj` the arm's active ones are used and reported.
- `GET /robot/rapid` – `robot.read`; execution state, motor/op mode and the program modules in each task.
- `PUT /robot/rapid/tasks/{task}/modules/{module}` (`{"source": "..."}`, must declare `MODULE <module>`, ISO-8859-1, replaces a same-named module), `DELETE` the same path, `POST /robot/rapid/start` (`{"reset_program_pointer": bool}`), `POST /robot/rapid/stop` – `robot.control` **and** the control lease. Load/unload return 404 for an unknown task and 409 while RAPID runs. They publish `robot.rapid.loaded|unloaded|started|stopped` (subscribe to `robot.rapid`).
- `POST /robot/rws/move` (`{"x","y","z"}` mm, optional `"rx","ry","rz"` degrees, all three or none, same meaning as RAPID `OrientZYX(rz, ry, rx)`) and `POST /robot/rws/home` – `robot.control` **and** the lease. `rws_motion` ([gateway/rws/motion.py](gateway/rws/motion.py)) writes the PERS variables of `gateway/rapid/LegacyShared.mod` in `GATEWAY_RWS_MOTION_TASK` (default `T_ROB_L`; paths `/rw/rapid/symbol/data/RAPID/<task>/<var>`, no mastership – the controller refuses it while RAPID runs), sets `move_request`, polls it every 50 ms until RAPID clears it, then reads `move_ok` / `move_error`. Returns the legacy `CommandResponse` (`DONE`, or `accepted: false` with `ERR <move_error>`); 409 if RAPID is not running, the variables are missing, or a move is in progress (one at a time, also after a timeout until RAPID finishes); 504 after `GATEWAY_RWS_MOVE_TIMEOUT` (60 s). Publishes `robot.status` like the tcp provider. With the real `GATEWAY_ROBOT_HOST` these move the real robot – never call them as a test without the user asking.

### RAPID program

[gateway/rapid/](gateway/rapid/) holds the gateway's robot-side program, driven over RWS (there is no socket server in it). `LegacyShared.mod` declares the command PERS variables (`move_request`, `go_home`, `move_ok`, `move_error`, `use_rot`, `target_pos`, `target_rot`) and is loaded into both tasks. `LegacyMotion.mod` (`T_ROB_L`) waits for `move_request`, checks the safe box (`X/Y/Z_MIN/MAX`, tool0 in wobj0), moves and reports. `RightArmIdle.mod` is only there because RAPID will not start while a task has no `main`. Keep RAPID changes minimal: other people work on the RAPID code.

`python gateway/rapid/upload_rapid.py` uploads them over RWS (unloading every program module in both tasks first) and starts the program; it defaults to the RobotStudio virtual controller on `127.0.0.1:80`. The legacy socket server (`x,y,z[,rx,ry,rz]` / `HOME`, replies `DONE` / `ERR ...`) now only exists as the test fixture [gateway/tests/rapid/LegacyCommServer.mod](gateway/tests/rapid/LegacyCommServer.mod); to drive it through the gateway's `tcp` provider, load it into `T_ROB_R` with `SERVER_IP` set to the controller address and run the gateway with `GATEWAY_ROBOT_HOST=127.0.0.1 GATEWAY_ROBOT_PORT=5000`.

## Things to be aware of

- `gateway/.env` and `gateway/auth_clients.json` are tracked in git even though `.env` is listed in `.gitignore`, so edits to them show up as changes.
- There is no `.dockerignore` (it was removed because it caused problems with the Westermo build), so `COPY gateway ./gateway` puts `gateway/.env` and `gateway/auth_clients.json` into the image as they are at build time.
- Configuration is `GATEWAY_`-prefixed env vars / `gateway/.env`, defined in [gateway/config.py](gateway/config.py) and cached by `get_settings()` (`lru_cache`), so changes require a restart.
- The code style is heavily vertical (one argument per line, `# ====` section banners); match it when editing gateway files.
