# Gateway starter

## Install

```bash
python -m venv .venv
```

Activate the environment, then:

```bash
python -m pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env`. For the starter, keep:

```text
GATEWAY_SECURITY_MODE=development
```

Development mode intentionally grants a local development identity all application permissions. Do not expose it to an untrusted network.

## Run

From the directory containing the `gateway` folder:

```bash
uvicorn gateway.main:app --host 127.0.0.1 --port 8000 --reload
```

Open:

- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/api/v1/health (`ok` / `degraded`, with camera and robot controller details)
- Robot status: http://127.0.0.1:8000/api/v1/robot/status
- Arm poses: http://127.0.0.1:8000/api/v1/robot/arms
- RAPID status: http://127.0.0.1:8000/api/v1/robot/rapid

The arm, RAPID and RWS move endpoints talk to the controller over Robot Web Services at `GATEWAY_ROBOT_HOST:GATEWAY_RWS_PORT` (default the real YuMi, `192.168.125.1:80`); the legacy `/robot/command` uses the same host with `GATEWAY_ROBOT_PORT`. To use the RobotStudio virtual controller instead, start the gateway with `GATEWAY_ROBOT_HOST=127.0.0.1` (and `GATEWAY_ROBOT_PORT=5000` for the legacy socket server).

There are two ways to move the robot:

- `POST /api/v1/robot/rws/move` (`{"x","y","z"}` mm, optional `"rx","ry","rz"` degrees, optional `"arm": "left"|"right"`, optional `"speed"` mm/s (default 50) and `"acceleration"`/`"ramp"` % (default 100)), `POST /api/v1/robot/rws/home` (optional `{"arm"}`, same speed fields) `GET /api/v1/robot/arms/left|right/gripper` reads where the gripper's fingers are (mm, 0 closed to 25 open; after a grip, how far it closed on the object), and `POST /api/v1/robot/rws/gripper` (`{"action": "grip"|"open"|"move"}`, optional `arm`; `move` needs `"position"`, the opening between the fingers in mm, 0 closed to 25 fully open; `grip` takes an optional `"force"` in N, 0 < force ≤ 20, default 20) write the job into the RAPID program in `rapid/` (its arm modules) over RWS and wait for it; with `"wait": false` they answer `STARTED` at once and `GET /api/v1/robot/rws/status?arm=left|right` reports `BUSY` / `DONE` / `ERR move failed`, like the team's CommServer. Targets outside `GATEWAY_RWS_SAFE_ZONE_MIN/MAX` are refused by the gateway. Load and start the program with `python rapid/rapidctl.py upload` (defaults to the real YuMi, which must be in AUTO; it backs up the modules it replaces; `--host 127.0.0.1 upload --gripper-stub` for RobotStudio). No socket server is involved.
- `POST /api/v1/robot/command` (deprecated but still working; legacy, `GATEWAY_ROBOT_PROVIDER=tcp`) sends the command text to a RAPID socket server on `GATEWAY_ROBOT_HOST:GATEWAY_ROBOT_PORT`.

The custom web app's **Robot controls** panel also uses these administration routes:

| Route | Effect |
|---|---|
| `GET /api/v1/robot/rapid/collision` | Read collision acknowledgement state; requires `robot.read`. |
| `POST /api/v1/robot/rapid/collision/ack` | Request collision acknowledgement with RAPID stopped; requires `robot.control` and the active lease. No motor enable, start or home. ABB can reject remote clients without Local Client privileges. |
| `POST /api/v1/robot/rapid/reset` | With `start: false` (default), stop RAPID, clear/read back pending arm requests, reset PP to main and leave stopped. Requires `robot.control` and the active lease. |

For explicit initialization, the reset route accepts `start: true` with the
`stop_generation` value just read from `GET /api/v1/robot/rapid`. A new Stop
invalidates that value. Initialization requires AUTO, motors already on, and a
cleared collision state; main may calibrate the grippers. The web app always
binds reset/initialization to this value and never falls back to resuming an
interrupted program. This is separate from app recovery acknowledgement and
homing. No route disables collision supervision or enables motors.

Both need `robot.control` and the control lease. `python gateway/scripts/basic_client.py` shows the RWS route from a client: it loads and starts the program through the gateway and runs a short demo path.

Example mock command:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/robot/command -H 'Content-Type: application/json' -d 'WAYEND,515.0,200.0,250.0'
```

With `GATEWAY_ROBOT_PROVIDER=mock` the robot service does not contact physical hardware.

## Session fencing for the MacBook / Pi inference deployment

Set `GATEWAY_CONTROL_REQUIRE_SESSION=true` for the Pi's single robot executor.
Run **one gateway process / Uvicorn worker**: control ownership is managed in that
process. The default remains `false` for existing clients. No inference-provider
switch acquires, releases or transfers the executor's robot lease.

`POST /api/v1/control/acquire` returns a new `lease.session_id` for every
acquisition. Send it as `X-Control-Session` on renewal, release, and all protected
robot/calibration actions. Renewal preserves the session. A stale supplied
session is rejected even when mandatory sessions are disabled. Sharing a bearer
token does not authorize a request from a previous acquisition.

`GET /api/v1/control` reports `session_required`, `watchdog_enabled`,
`recovery_required`, and `recovery_reason`. Check these before enabling the live
executor. Startup requires deliberate recovery: inspect the robot, acquire a
session, and use the existing explicit RAPID reset procedure. The verified reset
clears both arms' pending flags and resets the program pointer before releasing
the gateway recovery block. A failed reset leaves that block in place.

In this mode an active watchdog uses a monotonic lease deadline. Expiry,
preemption, or release during an outstanding robot action immediately fences
pending dispatch, latches recovery, and requests a software stop through RWS.
The stop result is verified; failure is reported as **UNCONFIRMED**. New movement
and ordinary RAPID start remain blocked until verified reset. An explicit reset
waits until any watchdog stop attempt has finished. A clean release after
completed commands does not require another reset.

The final arm request flag is serialized against lease invalidation and software
Stop; long movements do not hold the lease manager lock. Legacy socket commands,
`wait=false` jobs, and module upload/unload are refused in this mode. Install the
RAPID modules in a stopped maintenance deployment before enabling session fencing.
The existing explicit `reset` with `start=true` remains a separately reviewed
initialization action; inference recovery never starts RAPID or opens grippers.

These are application safeguards, not independent robot safety controls. Gateway
or Pi loss can prevent the software stop from reaching the controller. Validate
the controller-side stop and physical recovery procedure before live experiments.
The new session/watchdog tests use fake RWS controllers; they do not demonstrate
physical stopping performance.

## Tests

```bash
pip install -r gateway/requirements-dev.txt
python -m pytest                  # mocks only, no hardware needed
python -m pytest --robotstudio    # also the RobotStudio virtual controller on 127.0.0.1
python -m pytest --oak            # also the real OAK camera
```

The tests start their own gateways on free ports, so nothing needs to be running first. Client tools (image capture, event watcher, client template, token generator) are in `gateway/scripts/`.

## Run in Docker

Run every command from the repository root (the directory containing the `gateway` folder), inside WSL or on Linux. If your user is not in the `docker` group, prefix the commands with `sudo`.

The image contains `gateway/.env` and `gateway/auth_clients.json` as they are at build time, so the container starts with the security mode and camera provider set there. Rebuild after changing either file or any code.

### Build

```bash
docker build -f gateway/docker/DockerFile -t robot-gateway:local .
```

### Start

First start (creates the container):

```bash
docker run -d --restart unless-stopped --name robot-gateway -p 8000:8000 robot-gateway:local
```

To run without the OAK camera connected, use the mock camera instead:

```bash
docker run -d --restart unless-stopped --name robot-gateway -p 8000:8000 -e GATEWAY_CAMERA_PROVIDER=mock robot-gateway:local
```

Any `GATEWAY_*` setting can be overridden the same way with `-e`.

### Stop, start again, restart

```bash
docker stop robot-gateway
docker start robot-gateway
docker restart robot-gateway
```

### Logs and status

```bash
docker ps --filter name=robot-gateway
docker logs -f robot-gateway
```

### Rebuild after a change

```bash
docker build -f gateway/docker/DockerFile -t robot-gateway:local .
docker rm -f robot-gateway
docker run -d --restart unless-stopped --name robot-gateway -p 8000:8000 robot-gateway:local
```

### Check that it is running

```bash
curl http://127.0.0.1:8000/api/v1/health
```

API docs are at http://127.0.0.1:8000/docs. In `strict` mode every other endpoint needs `Authorization: Bearer <token>`.

### WSL notes

- WSL shuts its VM down when no WSL terminal is open, which stops Docker and the container. Keep a WSL terminal open while the gateway should be running. With `--restart unless-stopped` the container comes back when WSL and Docker start again.
- The published port is only reachable from the Windows host itself. To let other machines on the network connect, forward the port from an administrator PowerShell, where `<host-ip>` is the Windows address on that network and `<wsl-ip>` is the output of `wsl hostname -I` (first address):

  ```powershell
  netsh interface portproxy add v4tov4 listenaddress=<host-ip> listenport=8000 connectaddress=<wsl-ip> connectport=8000
  New-NetFirewallRule -DisplayName "Robot Gateway 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -LocalAddress <host-ip> -Action Allow
  ```

  The WSL address changes when WSL restarts. If other machines lose the connection, delete the forwarding rule and add it again with the new address:

  ```powershell
  netsh interface portproxy delete v4tov4 listenaddress=<host-ip> listenport=8000
  ```

## Deploy to the Raspberry Pi

In the lab the gateway runs in Docker on the Raspberry Pi 5 on the switch. The Pi has no internet access, so the image is built on a PC, copied over and started there.

| Device | Address |
|---|---|
| Raspberry Pi (gateway) | `169.254.125.202` (fixed) and `192.168.125.201` (assigned automatically, can change) |
| OAK camera | `169.254.1.223` |
| YuMi robot | `192.168.125.1`, port `5001` |

Clients reach the gateway at `http://169.254.125.202:8000` or `http://192.168.125.201:8000`, depending on which network they are on.

Log in to the Pi as `kasun`; ask the team for the password.

### 1. Build the image (on your PC)

Run in WSL or on Linux, from the repository root. The image contains `gateway/.env` and `gateway/auth_clients.json` as they are now, so check `gateway/.env` first. On the Pi it should use `GATEWAY_SECURITY_MODE=strict`, `GATEWAY_CAMERA_PROVIDER=oak` and `GATEWAY_ROBOT_PROVIDER=tcp`.

```bash
docker build --platform linux/arm64 -f gateway/docker/DockerFile -t robot-gateway:pi .
mkdir -p gateway/docker/images
docker save robot-gateway:pi | gzip > gateway/docker/images/robot-gateway-pi-arm64.tar.gz
```

The Pi uses an arm64 processor. On an arm64 PC this builds directly; on a normal x86 PC, enable arm64 emulation once first:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

`gateway/docker/images/` is ignored by git, so the archive is never committed.

### 2. Copy it to the Pi

From PowerShell or WSL, at the repository root:

```bash
scp gateway/docker/images/robot-gateway-pi-arm64.tar.gz kasun@192.168.125.201:~/
```

### 3. Load and start it (on the Pi)

```bash
ssh kasun@192.168.125.201

sudo docker load -i ~/robot-gateway-pi-arm64.tar.gz
sudo docker rm -f robot-gateway
sudo docker run -d --name robot-gateway --restart unless-stopped --network host robot-gateway:pi
rm ~/robot-gateway-pi-arm64.tar.gz
```

`--network host` lets the container use the Pi's own network addresses. That is needed to reach the camera, and it makes the gateway reachable on both Pi addresses without port forwarding. `--restart unless-stopped` starts it again after a crash or a reboot.

### 4. Check that it is running

On the Pi, follow the log until it shows `Application startup complete` (connecting to the camera can take up to a minute):

```bash
sudo docker logs -f robot-gateway
```

From any machine on the switch:

```bash
curl http://169.254.125.202:8000/api/v1/health
```

### Things to know

- Only one program can use the camera at a time. Stop any other gateway, for example one running on a laptop, before starting the one on the Pi.
- Stop, start, restart and logs work as in the Docker section above, with `sudo` in front.
- The Pi's SD card is small. Each image takes about 720 MB once loaded; check free space with `df -h /`. `sudo docker image prune -f` removes old untagged images left behind by earlier deployments.
- The Pi's `169.254.125.202` address is already set up and survives reboots. If the Pi is ever reinstalled, add it again with:

  ```bash
  sudo nmcli con mod netplan-eth0 +ipv4.addresses 169.254.125.202/16
  sudo ip addr add 169.254.125.202/16 dev eth0
  ```
