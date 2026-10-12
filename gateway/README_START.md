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

Example mock command:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/robot/command -H 'Content-Type: application/json' -d '{"command":"hello","parameters":{}}'
```

The robot service is a mock and does not contact physical hardware. The next implementation phase is control ownership, priority, lease renewal/expiration, and event notification.

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
