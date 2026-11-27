# Westermo Lynx (WeOS) — lab switch & edge-AI host

This folder documents the **Westermo Lynx** industrial switch that sits on the lab
network over the laptop's USB‑to‑Ethernet dongle ("Ethernet 2"). In the fail‑safe
chain it is the **Lynx tier** (lowest AI tier, see [CLAUDE.md](../CLAUDE.md)) — it
both switches the lab traffic and *hosts* the Lynx AI model as a WeOS container app.

Everything here was discovered live over SSH with the device's own `help`, `tree`
and `show` commands on **2026-11-11**.

- [COMMANDS.md](COMMANDS.md) — the full WeOS CLI command reference and how to use it.
- [DEPLOY.md](DEPLOY.md) — how to build, import, configure and run any app on the Lynx
  (the C++ gateway's deployment is [gateway/cpp/LYNX.md](../gateway/cpp/LYNX.md)).
- [command-tree.txt](command-tree.txt) — raw `tree` output: every command and config node.
- `running-config.sanitized.json` — the device's live config as of 2026-11-11, with the
  admin password hash and SNMP community strings redacted. **Kept local only — it is
  git-ignored and not exposed in the repo for security reasons** (it is still a full
  snapshot of a shared device's topology, services and app layout). Regenerate it on
  demand with `show running-config` over SSH (see below).

## The device

| | |
|---|---|
| Product | **Lynx‑3510‑E‑F2G‑P8G‑LV** (10‑port, 2×SFP + 8×GbE, PoE, Extended class) |
| Hostname | `lynx-91-13-c0` |
| Firmware | **WeOS v5.30.0** (codename *dolphin*), bootloader 2024.03.0‑5 |
| Platform / arch | Envoy / **ARMv8 (arm64)** |
| Article / serial | 3627‑0510‑006 / 6863 |
| Base MAC | `00:11:b4:91:13:c0` (Westermo OUI `00:11:B4`) |
| Mfg date | 2024‑09‑06 |

## How to reach it

Three VLANs since 2026-11-25 (before, one flat `vlan1`):

```
vlan1   eth1, eth2, eth5..eth10   169.254.219.127/16 (link-local) + 192.168.125.20/24 (static)
vlan10  eth3 ("camera")           169.254.1.200/26   the OAK 169.254.1.223
vlan20  eth4 ("robot")            192.168.125.2/30   the YuMi 192.168.125.1
```

IP forwarding is off, so only the Lynx itself and apps on it with
`netns host-os` (the gateway) reach the camera and the robot. Manage the
Lynx and reach the gateway on `169.254.219.127` or `192.168.125.20` as
before. Details, the commands and what it broke: "Network isolation" in
[gateway/cpp/LYNX.md](../gateway/cpp/LYNX.md#network-isolation).
The configuration is saved (2026-11-26, after the internal flash
was freed, see there). Install app images with `file app import`, never
`package install`, which fills the 14 MB internal flash. Management
services enabled: **SSH (22), HTTP (80), HTTPS (443), Telnet (23), SNMP**.

### SSH in

Credentials are the WeOS factory login **`admin` / `admin`** (change these — see
"Hardening" in COMMANDS.md).

```powershell
# OpenSSH (Git Bash / PowerShell)
ssh admin@169.254.219.127

# PuTTY plink (already installed on the laptop), non-interactive / scriptable:
& "C:\Program Files\PuTTY\plink.exe" -ssh -batch `
    -hostkey SHA256:EGKWPhMgqZMo7I6c4XrIsIBGimY75jOTXK92QX8FUz8 `
    -pw admin admin@169.254.219.127 "show system-information"
```

Host‑key fingerprint (ECDSA): `SHA256:EGKWPhMgqZMo7I6c4XrIsIBGimY75jOTXK92QX8FUz8`.

### Web UI

`https://169.254.219.127/` (self‑signed cert; page title `lynx-91-13-c0`).

## Why this device matters to the project

The Lynx is **already hosting the project's edge‑AI apps** via WeOS *app‑hosting*
(Docker‑style containers running on the switch itself). From the live config:

| App | Image | Purpose (inferred) | State |
|---|---|---|---|
| `lynxpicker` | `lynx-picker-0.1.1` | The Lynx‑tier picker; `GATEWAY_CAMERA_IP=169.254.1.223`, mounts `/state` and `/images` | Stopped |
| `yololivetest` | `envoy-yololive-1.0` | YOLO live inference off the OAK (`OAK_TARGET=169.254.1.223`) | Created |
| `yolowebtest` | `envoy-yoloweb-1.0` | YOLO + web viewer on port 8080 | Created |
| `yolotest` | `yolophoto2` | YOLO still‑image test (own netns) | No image |
| `gateway` | `gateway-cpp-0.5.0` (label `gatewaycpp`) | The C++ robot gateway, port 8000 ([gateway/cpp/LYNX.md](../gateway/cpp/LYNX.md)) | Running (saved in startup-config, autostart) |

`camnettest` (NCNN cam test, image `yoloncnn` no longer present) was removed on 2026-11-11 to make room: WeOS allows 5 apps. The same day the running config was saved to the startup config; that also made permanent an earlier unsaved change by someone else (`lynxpicker` had replaced the hello-world app `app1`: image `hello-lynx-1.0`, `cmd /hello.sh`, own netns, autostart).

Images live on **external media** (`external`, path `app/images`), and the apps
reach the **OAK‑D camera at `169.254.1.223`** — the same camera the gateway uses.
See the "App hosting" section of [COMMANDS.md](COMMANDS.md) for how to import a
`.wmopkg`, start/stop an app, and read its logs. The gateway's C++ port
([gateway/cpp/](../gateway/cpp/)) is the code targeted at running here.

> ⚠️ This is **live lab shared hardware**. `reboot`, `factory-reset`, firmware
> `upgrade`, and config changes affect everyone on the switch and can drop the
> robot/camera/Pi links. Read-only `show` commands are safe; treat everything
> else as a change that needs coordination.
