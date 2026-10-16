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
- Health: http://127.0.0.1:8000/api/v1/health
- Robot status: http://127.0.0.1:8000/api/v1/robot/status
- Arm poses: http://127.0.0.1:8000/api/v1/robot/arms
- RAPID status: http://127.0.0.1:8000/api/v1/robot/rapid

The arm and RAPID endpoints talk to the controller over Robot Web Services at `GATEWAY_RWS_HOST` (default the real YuMi, `192.168.125.1`). To use the RobotStudio virtual controller instead, start the gateway with `GATEWAY_RWS_HOST=127.0.0.1` (and `GATEWAY_ROBOT_HOST=127.0.0.1 GATEWAY_ROBOT_PORT=5000` for robot commands).

Example mock command:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/robot/command -H 'Content-Type: application/json' -d 'WAYEND,515.0,200.0,250.0'
```

With `GATEWAY_ROBOT_PROVIDER=mock` the robot service does not contact physical hardware.

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
| Raspberry Pi (gateway) | `169.254.125.202` (fixed) and `192.168.125.202` (assigned automatically, can change) |
| OAK camera | `169.254.1.223` |
| YuMi robot | `192.168.125.1`, port `5001` |

Clients reach the gateway at `http://169.254.125.202:8000` or `http://192.168.125.202:8000`, depending on which network they are on.

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
scp gateway/docker/images/robot-gateway-pi-arm64.tar.gz kasun@192.168.125.202:~/
```

### 3. Load and start it (on the Pi)

```bash
ssh kasun@192.168.125.202

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
