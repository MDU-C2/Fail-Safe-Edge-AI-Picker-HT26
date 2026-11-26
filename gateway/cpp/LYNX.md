# Deploying the C++ gateway to the Westermo Lynx

How the C++ gateway gets onto the lab's Lynx and runs there as a WeOS
app, with every step as it was done and tested on 2026-11-11 (Lynx-3510,
WeOS 5.30.0, arm64, `lynx-91-13-c0` at `169.254.219.127`). The device
itself, its CLI and a generic deployment for any app are in
[lynx-docs/](../../lynx-docs/README.md); this file is what is specific to the
gateway.

**Short version**, from the repository root:

```powershell
wsl -d Ubuntu-22.04 -u root -- gateway/cpp/dev.sh wmopkg      # build  -> gateway/cpp/dist/gateway-cpp-0.5.0.wmopkg
$env:LYNX_PASSWORD = "<Lynx admin password>"
powershell -ExecutionPolicy Bypass -File gateway/cpp/tools/deploy_lynx.ps1   # import, configure, start, wait for /health
python gateway/scripts/gatewayctl.py --target lynx --token <robot.control token> smoke --read-token <camera.read token>
```

The gateway is then at `http://169.254.219.127:8000/api/v1` (strict mode,
the clients of `gateway/auth_clients.json`, see [Clients](#clients)).
It is also at `http://192.168.125.20:8000/api/v1` for clients on the
`192.168.125.x` subnet. Since 2026-11-25 the robot and the camera are on
VLANs of their own and only the gateway reaches them, see
[Network isolation](#network-isolation).

> The Lynx is shared lab hardware. Deploying adds an image and one app
> (`gateway`); it changes nothing else. Only one process can hold the
> OAK camera, so stop any other gateway using it first (the one on the
> Raspberry Pi: `sudo docker stop robot-gateway` there).

## What runs on the Lynx

| | |
|---|---|
| Package | `gateway-cpp-<version>.wmopkg`, ~12 MB: an xz squashfs of the runtime image in a FIT image, unsigned |
| Contents | the static release binary (arm64, depthai-core built in) at `/sbin/init`, `gateway/auth_clients.json` (token hashes only, see [Clients](#clients)) |
| Image label | `gatewaycpp` (the app refers to the label, so a new version is a label move) |
| App | `gateway`: `cmd "/sbin/init --host 0.0.0.0 --port 8000"`, `netns host-os` (the Lynx's own addresses and the camera's link-local network), share `external/app/gateway/state` → `/state` (created), environment `GATEWAY_CAMERA_PROVIDER=oak`, `GATEWAY_CAMERA_IP=169.254.1.223`, `GATEWAY_CALIBRATION_FILE=/state/calibration.json`, `GATEWAY_RWS_SAFE_ZONE_MIN=[180,-400,50]`, `GATEWAY_RWS_SAFE_ZONE_MAX=[800,500,700]` (see [Safe zone](#safe-zone)), `GATEWAY_RWS_HEARTBEAT_TIMEOUT=5` (from the deploy script since 0.6.0; the 0.6.0 build deployed on 2026-11-23 predates it), `GATEWAY_CAMERA_UNDISTORT=true` (since 0.8.0, `-Undistort false` in the deploy script for raw frames) and an empty `GATEWAY_AUTH_CLIENTS` (so the package's clients file is used, see [Clients](#clients)); enabled, autostart (since 2026-11-11); saved in the startup config, so it starts when the Lynx boots. Redeploy with `-Autostart -Save` to keep that, since the script otherwise sets `no autostart` |
| Everything else | the binary's defaults, the same as the Python gateway's: strict security, `GATEWAY_ROBOT_HOST=192.168.125.1` (the real YuMi), 1920x1080 RGB at 4 fps |

The image is read-only, so the only file the gateway writes, the
calibration, lives on the `/state` share (external media); it survives
restarts and redeploys. Settings are environment variables on the
app (no `.env` in the image), so they change without a new package; the
clients are in the package (see [Clients](#clients)).

## 1. Build the package

```sh
gateway/cpp/dev.sh wmopkg [VERSION]       # in WSL; VERSION defaults to the one in CMakeLists.txt
```

What it does (`dev.sh`, command `wmopkg`):

1. `docker build --platform linux/arm64 -f gateway/cpp/docker/Dockerfile --target runtime -t gateway-cpp .`
   — the static release binary (built and unit-tested in the `build`
   stage) in a `FROM scratch` image, plus the clients file;
2. `docker create` + `docker export` → `gateway/cpp/dist/rootfs.tar`;
3. Westermo's [gateway/docker/generate-container.py](../docker/generate-container.py)
   `-t rootfs` in a small Debian container with `mksquashfs`, `mkimage`,
   `dtc` and `file` (image `gateway-cpp-wmopkg-tools`) →
   `gateway/cpp/dist/gateway-cpp-<VERSION>.wmopkg`.

Why the rootfs route rather than the generator's `-t docker`: a
`.wmopkg` keeps only the filesystem, never `ENTRYPOINT`/`CMD`, so
nothing is lost by exporting, and the generator's own `docker buildx`
build would start without our build cache (depthai-core takes ~6 min).
The laptop is arm64, so no emulation is involved.

`gateway/cpp/dist/` is git-ignored.

## 2. Deploy with the script

[tools/deploy_lynx.ps1](tools/deploy_lynx.ps1), on the lab laptop
(Windows, PuTTY's `plink` on `PATH`, connected to the lab network):

```powershell
$env:LYNX_PASSWORD = "<Lynx admin password>"
powershell -ExecutionPolicy Bypass -File gateway/cpp/tools/deploy_lynx.ps1 [-Version 0.5.0] [-Autostart] [-Save]
```

| Option | Default | |
|---|---|---|
| `-Lynx`, `-User`, `-HostKey` | `169.254.219.127`, `admin`, the Lynx's ECDSA key | |
| `-Password` | `$env:LYNX_PASSWORD` | never stored in the script |
| `-Version` | from `CMakeLists.txt` | which `dist/gateway-cpp-<version>.wmopkg` |
| `-App`, `-Label` | `gateway`, `gatewaycpp` | |
| `-CameraIp`, `-Port` | `169.254.1.223`, `8000` | |
| `-SafeZoneMin`, `-SafeZoneMax` | `[180,-400,50]`, `[800,500,700]` | the gateway's safe zone for RWS moves, as JSON lists (see [Safe zone](#safe-zone)) |
| `-Autostart` | off | start the app when the Lynx boots |
| `-Save` | off | `copy running-config startup-config`, so the app definition survives a reboot. This saves the **whole** running config, including anyone else's unsaved changes, hence opt-in |

It serves the package from the laptop, imports, labels, configures,
restarts the app and waits up to 120 s for `/health` to report the
camera connected, printing the app log if it does not. Running it again
redeploys (the same version is replaced in place).

## 3. Deploy by hand

The same steps without the script. SSH to the Lynx with
`ssh admin@169.254.219.127` (or `plink`, see [lynx-docs/README.md](../../lynx-docs/README.md)).

**Serve the package from the laptop.** The Lynx pulls files (FTP, TFTP,
SCP, SFTP, HTTP, HTTPS); scp *to* it is switched off
(`management ssh external-access`), so serve the file from the laptop's
address on the lab network (`169.254.125.10`):

```powershell
.venv\Scripts\python.exe -m http.server 8099 --bind 169.254.125.10 --directory gateway/cpp/dist
```

**Import the image and label it** (EXEC mode):

```
file app import proto http 169.254.125.10:8099 /gateway-cpp-0.5.0.wmopkg
app-hosting label gateway-cpp-0.5.0 gatewaycpp
show app-hosting
```

The import logs "File signature check failed" (the package is unsigned)
and still installs it: `show app-hosting` lists it `VALID Yes,
VERIFIED No`, like every other image on this Lynx. Stop the HTTP server
afterwards.

**Define the app** (configuration mode; `leave` applies it):

```
configure
app-hosting
app gateway
description "Robot gateway (C++) 0.5.0"
image gatewaycpp
cmd "/sbin/init --host 0.0.0.0 --port 8000"
netns host-os
share media external app/gateway/state as /state create
environment GATEWAY_CAMERA_PROVIDER oak
environment GATEWAY_CAMERA_IP 169.254.1.223
environment GATEWAY_CALIBRATION_FILE /state/calibration.json
environment GATEWAY_RWS_SAFE_ZONE_MIN [180,-400,50]
environment GATEWAY_RWS_SAFE_ZONE_MAX [800,500,700]
environment GATEWAY_RWS_HEARTBEAT_TIMEOUT 5
environment GATEWAY_CAMERA_UNDISTORT true
enable
no autostart
leave
```

Optionally persist it (saves the whole running config):
`copy running-config startup-config`.

**Start it and watch it come up:**

```
app-hosting start gateway
show app-hosting                      # gateway: Running
show log app-gateway.log              # the gateway's output (not `app-hosting log`, which shows nothing)
```

and from the laptop, until `camera.connected` is true (~10–30 s):

```powershell
Invoke-RestMethod http://169.254.219.127:8000/api/v1/health
```

## 4. Operate

| | |
|---|---|
| Stop / start / restart | `app-hosting stop gateway`, `app-hosting start gateway`, `app-hosting restart gateway` |
| Status | `show app-hosting`, `app-hosting app gateway`, `GET /api/v1/health` |
| Gateway output | `show log app-gateway.log`; `follow app-gateway.log` to tail |
| Live events | `ws://169.254.219.127:8000/api/v1/events` with `Authorization: Bearer <token>` (0.4.0 and later): `python diagnostics/gateway_diag.py --target lynx watch` (dashboard + events) or `... --target lynx events` (the `diagnostics` token from `client_tokens.json`) |
| CPU / memory | `show processes` (the gateway is `/sbin/init --host 0.0.0.0 --port 8000`), `show memory` |
| Change a setting | `configure` → `app-hosting` → `app gateway` → `environment GATEWAY_<NAME> <value>` → `leave`, then `app-hosting restart gateway` |
| New version | `dev.sh wmopkg`, then the deploy script (or import + `app-hosting label gateway-cpp-<new> gatewaycpp` + `app-hosting reload gatewaycpp`) |
| Calibration file | `/state/calibration.json` in the app = `external/app/gateway/state` on the Lynx |
| Clients / tokens | `gateway/auth_clients.json` in the package: edit, build, deploy (see [Clients](#clients)) |

### Clients

The clients are **in the package**: `gateway/auth_clients.json` (token
hashes only) as it is when the package is built, at
`/gateway/auth_clients.json` in the image. To change a client or a
token (hash from `python gateway/scripts/gatewayctl.py token new <client>`),
edit that file, build a new package (`dev.sh wmopkg`, ideally with a new
version) and deploy it. The plaintext tokens stay in
`client_tokens.json` on the laptop.

> **Limitation: WeOS keeps only 63 characters of an app environment
> value.** Anything longer is cut without an error, at configuration
> time. The gateway can also take its clients from
> `GATEWAY_AUTH_CLIENTS` (one line: clients separated by spaces, each
> `id:control_priority:permissions:token_hash`), and 0.7.0 was first
> deployed that way on 2026-11-24: the ~600-character line was stored as
> its first 63 characters, the admin entry with its hash cut to 50 hex
> digits, and the gateway started with no client any token could match
> (every request 401). A single SHA-256 is already 64 hex digits, so no
> client fits in one value, hence the clients file in the package again.
> There is no other way to get a file to the app either (scp to the Lynx
> is off, WeOS's `file` commands handle only images, configs and logs,
> the image has no shell). Keep every other app environment value under
> 64 characters too (the safe zone and the other settings are far below).

Since then the gateway refuses a `token_hash` that is not 64 lowercase
hex digits, in the file and in `GATEWAY_AUTH_CLIENTS`, so a cut value
stops it at startup (`show log app-gateway.log` names the client and the
source) instead of locking everyone out. The deploy script sets
`GATEWAY_AUTH_CLIENTS` to an empty value on the app, which makes the
gateway use the clients file (and clears the cut line of that first
deploy). `GATEWAY_AUTH_CLIENTS` and `GATEWAY_AUTH_CLIENTS_FILE` still
replace the baked-in file elsewhere (Docker, tests).

### Safe zone

The gateway refuses RWS moves whose target (tool0 in the base frame, mm)
is outside the box `GATEWAY_RWS_SAFE_ZONE_MIN` .. `GATEWAY_RWS_SAFE_ZONE_MAX`
with `ERR outside safe zone`, before anything is written to the robot
(`home` is not checked). On the Lynx both are app environment variables,
so the zone can be changed without a new package:

```
configure
app-hosting
app gateway
environment GATEWAY_RWS_SAFE_ZONE_MIN [180,-400,50]
environment GATEWAY_RWS_SAFE_ZONE_MAX [800,500,700]
leave
app-hosting restart gateway
copy running-config startup-config    # keep it over a reboot (saves the whole running config)
```

Values are JSON lists of three numbers `[x,y,z]`, without spaces. A
malformed value stops the gateway at startup (`show log app-gateway.log`),
so check `/health` after the restart. The deploy script sets them from
`-SafeZoneMin` / `-SafeZoneMax`, so pass the current values when
redeploying after a change by hand.

Current zone (since 2026-11-11): x 180..800, y -400..500, z 50..**700**.
This matches the Pi's Python gateway, whose `gateway/.env` raised the top
from the code default 600 to 700; before that the Lynx used the defaults
(z up to 600).

### Deployed 2026-11-24: 0.7.1 and the RAPID program

0.7.1 (clients in the package, see [Clients](#clients)) replaced the
0.7.0 deploy that had cut the clients line; `smoke` with the operator
token passed (all checks, 99 % valid depth). Then the RAPID program of
`rapid/` was loaded through the gateway (old modules saved to
`rapid/backups/169.254.219.127-20261124-144014/`). The program on the
robot had no watchdog variables yet, so the gateway had not written its
session; started at once, the arms would have seen the session appear
just after their start and gone home. Hence, for a program that gains
the watchdog variables:

```powershell
python gateway/scripts/gatewayctl.py --target lynx --token <admin> -y rapid upload --no-start
# until /health robot_controller.watchdog.connected is true (one heartbeat, ~1 s)
python gateway/scripts/gatewayctl.py --target lynx --token <admin> rapid start --main
```

Neither arm went home; the primary tier took the lease and moved the
left arm moments later. `deploy_lynx.ps1 -Save` did not save that day
(the save line in the configuration session did not take effect); it
now runs `copy running-config startup-config` as its own command and
warns if the configs still differ.

### Network isolation

Since 2026-11-25 the robot and the camera each have a VLAN of their own
on the Lynx, so the gateway is the only way to them (RWS is plain HTTP,
CommServer on 5001 has no authentication, the OAK has no access control
at all):

| VLAN | Ports | Lynx address | On it |
|---|---|---|---|
| `vlan1` | eth1, eth2, eth5..eth10 | `169.254.219.127/16` (link-local), `192.168.125.20/24` (static) | everything else: laptops, Pi, lab PCs |
| `vlan10` "camera" | eth3 | `169.254.1.200/26` | the OAK `169.254.1.223` |
| `vlan20` "robot" | eth4 | `192.168.125.2/30` | the YuMi `192.168.125.1` (and two devices behind it, `.203`, `.250`) |

IP forwarding is off, so the Lynx routes nothing between them (the
firewall is still off). The camera and robot subnets sit inside the
`vlan1` ones on purpose: no device's address changes, clients keep the
gateway at `192.168.125.20` or `169.254.219.127`, and the Lynx reaches
`192.168.125.1` and `169.254.1.223` through the more specific route
(`/30`, `/26`) on the device's own port. The YuMi and the OAK still see
`.2` and `.200` as on-link (their own masks are wider) and answer
directly. The price: a client on `vlan1` with an address in
`192.168.125.0..3` or `169.254.1.192..255` gets no answer from the Lynx
(none had one). The `/26` is the smallest block where the camera's
`.223` is not the broadcast address. The gateway's settings did not
change.

Done with (each context left with `end`, applied with `leave`):

```
configure
vlan 1        → no untagged eth4, no untagged eth3
vlan 20       → name robot, untagged eth4
vlan 10       → name camera, untagged eth3
port eth4     → description robot
port eth3     → description camera
iface vlan20  → inet static 192.168.125.2/30
iface vlan10  → inet static 169.254.1.200/26
ip            → no forwarding
leave
```

`default-vid` is "not found" under `port` on this unit, and not needed:
a port's default VID follows the one VLAN it is untagged in. Undo: put
eth3/eth4 back with `untagged` in `vlan 1`, `no vlan 10`, `no vlan 20`,
`ip` → `forwarding`.

Checked from the laptop afterwards: `/health` ok on both gateway
addresses with camera and robot connected; the YuMi (ping, 80, 5001) and
the OAK (ping, 11490, 11491) do not answer. Right after a `leave` the
gateway may not answer for a few seconds.

What it breaks: everything that reached the robot or the camera
directly from `vlan1`: `rapid/rapidctl.py`, `legacy_socket_client.py`,
`capture_and_detect/where.py`, the picker's native controller status
(`controller_url` in `robot_controller.json`), RobotStudio online
against the real YuMi, DepthAI tools and `pytest --oak` against the
real OAK, and the camera apps `yololivetest`/`yolowebtest` (own network
namespace, `OAK_TARGET`). Plug into eth4 or eth3 for direct access.

> **Saved on 2026-11-26** (startup config = running config, 27 KB,
> with the VLANs and the gateway app). From 2026-11-25 it could not be
> saved: the internal `Config` partition (14 MB jffs2, `show media`) was
> 99 % full and the startup config on it cut off after ~4 KB. The cause
> was the picker's 47 MB `lynx-picker-0.2.9` installed on 2026-11-17 as
> a WeOS *system package* (`package install`, listed by `show package`
> with version "Unknown"), which writes to the internal flash, not as
> an app image (`file app import`, SD card). `package remove
> lynx-picker-0.2.9` freed it (99 % → 6 %; the list entry stays,
> "Operation not permitted", Persistent: No). The full flash also made
> `app-hosting label` fail silently, so a deploy kept the old image
> running (the deploy script now checks the version). Install app
> images with `file app import` only. Full running-config backups are
> kept off the repository (they hold secrets).

A laptop with Tailscale: its adapter also claims `169.254.0.0/16` (even
when disconnected), so Windows sends traffic for `169.254.219.127` there;
stop it (`Stop-Service Tailscale`, admin) while working with the Lynx.

## 5. Test

`smoke` of [gateway/scripts/gatewayctl.py](../scripts/gatewayctl.py) checks a
running gateway without moving the robot: `/health`, `/openapi.json`,
that a request without a token is refused, camera status, intrinsics,
an RGB and a depth frame (format, size, valid depth), robot status and
the control lease (acquire, renew, release; `--read-only` skips it).
`--write-calibration` also replaces the stored calibration with a test
object and reads it back.

```powershell
python gateway/scripts/gatewayctl.py --target lynx --token <robot.control token> `
    smoke --read-token <camera.read token> [--write-calibration]
```

Results on 2026-11-11 (gateway 0.3.0, real OAK at 1920x1080 / 4 fps):

| | |
|---|---|
| Smoke test | all 18 checks passed (with `--write-calibration`); 94 % valid depth. The same script passes against the Python gateway |
| Start to camera connected | 9 s on the first start, 26 s on a restart |
| Restart | calibration on `/state` kept |
| Load: 4 clients pulling RGB + depth for 30 s from the laptop | 22.5 frames/s (676 frames), 0 errors, gateway ~29 % CPU (~19 % idle) |
| Memory and storage | see "Footprint on the Lynx" below |
| Seccomp / AppArmor | WeOS's default profiles let it run as is (no extra capabilities) |

## Footprint on the Lynx

Measured on 2026-11-11 with gateway 0.3.0 running as app `gateway`,
the real OAK at 1920x1080 / 4 fps, the robot reachable over RWS.

**RAM.** WeOS shows no memory per app (`show metrics` has memory
metrics off, `show processes` gives only VSZ, 204 MB here, which is
address space, not memory), so this is the Lynx's own "used" memory
(`show memory`, i.e. without page cache) with the app stopped and
running, averaged over several samples:

| Lynx state | Lynx "used" | Gateway's share |
|---|---|---|
| Gateway stopped | 131.6 MB | – |
| Camera booting (peak of 12 samples over 15 s) | 185.1 MB | **+53.5 MB** |
| Idle, camera streaming | 171–179 MB | **+40–47 MB** |
| 4 clients pulling RGB + depth (22.3 frames/s for 40 s, 0 errors) | 159–168 MB | **+28–36 MB** |

The share includes the container runtime (`conmon`/`crun`, ~1–2 MB).
Page cache stayed at ~200 MB throughout: the binary's code (~12 MB,
read from the image's squashfs) is cached, not "used", and reclaimable.
Under load the gateway uses slightly less than idle: freed frames go
straight back to the system (the malloc settings in `main.cpp`), while
idle the newest frame set sits in memory. Within a few MB this matches
the laptop measurement of the same binary (39 MB idle, 43 MB under
load, 67 MB while booting; [README.md](README.md#memory-with-the-camera)).

Against the Lynx's budget: 496 MB in total, app hosting limited to 50 %
(~248 MB) for all apps together on this unit (WeOS default 25 %,
~124 MB), the OS itself ~132 MB. The gateway takes about a fifth of the
apps' share at its peak, against the Python gateway's 531 MB on the
Raspberry Pi, more than the whole Lynx.

**Storage.**

| Where | What | Size |
|---|---|---|
| External media (SD card `mmcblk1p1`, 29.1 GB, 4 % used) | the image `gateway-cpp-0.3.0`: the imported `.wmopkg` (xz squashfs with the 15.4 MB static binary and the clients file) | **11.9 MB** (11 920 798 bytes, the package's size; WeOS lists no sizes for images on external media) |
| External media, `app/gateway/state` | `calibration.json` (the `/state` share) | bytes to kB (at most 1 MB, the gateway's upload limit) |
| Internal flash (`Config`, 14 MB jffs2, 5 % used) | the app definition in the startup config | well under 1 kB |
| Primary / Secondary firmware (56 MB each) | – | untouched |

For comparison, the Python gateway's package is 146 MB (104 MB with its
junk removed), above the ~100 MB where web uploads to the Lynx fail.
Each imported version takes another ~12 MB on the SD card until removed
(`file app erase` cannot remove images on external media on WeOS 5.30,
see below).

## Things found on the way

- **At most 5 apps.** A sixth is refused, visible only in `show log`
  ("Failed creating app gateway", "Max number of apps reached"); the
  config session prints no error. The deploy script
  checks that the app exists afterwards. On 2026-11-11 the app
  `camnettest` (image `yoloncnn` no longer on the device, so it could
  not run) was removed with `no app camnettest` to make room; its
  definition was: image label `yoloncnn`, `cmd /start.sh`, `shell sh`,
  host network, capability `CAP_NET_RAW`.
- **A share must exist or carry `create`.** Without it the app shows
  "Dependency fail" and does not start ("Shared media path does not
  currently exist and is not marked with the 'create' flag").
- **No `ENTRYPOINT` in a `.wmopkg`.** WeOS starts `cmd` (default
  `/sbin/init` with no arguments); the gateway would then listen on
  127.0.0.1 only, hence `cmd "/sbin/init --host 0.0.0.0 --port 8000"`.
- **`file app erase` / `file app dir` do not see images on external
  media** (WeOS 5.30: "Path does not exist", an empty list). Importing
  the same version again replaces it; older versions stay
  (`gateway-cpp-0.1.0`, the first test import, is still there; remove
  it in the web UI if needed).
- **The Lynx's clock starts at 1970** (no time source reachable on the lab
  network), so the gateway's timestamps (`X-Frame-Timestamp`, lease and
  calibration times) are in January 1970 until it is set. Lease timing is
  unaffected (it runs on the monotonic clock). Set it by hand in local time
  (timezone `Europe/Stockholm`, saved 2026-11-11) with the EXEC command
  `date YYYY-MM-DD hh:mm:ss`, e.g. from the laptop (see
  [lynx-docs/DEPLOY.md](../../lynx-docs/DEPLOY.md)); it was set on 2026-11-11 and is
  most likely lost on a reboot.
- **The robot needs a 192.168.125.x address on the Lynx.** The YuMi
  is cabled to the Lynx but only answers on `192.168.125.1`, and the
  Lynx had only its link-local address (DHCP gets no answer), so
  `/health` was `degraded` and the RWS routes answered 503. Fixed on
  2026-11-11 by adding a static address next to the link-local one
  (`configure` → `iface vlan1` → `inet static 192.168.125.20/24` →
  `leave`); since then `/health` is `ok` with the controller reachable.
  Saved to the startup config together with the app the same day.
  Since 2026-11-25 the robot has its own VLAN, see
  [Network isolation](#network-isolation).
- **Piping commands into the CLI.** The deploy script feeds the config
  lines to an SSH session's standard input; Windows PowerShell may put a
  byte-order mark in front, which turns the first command into "not
  found", hence a leading empty line. Errors in such a session only show
  up in `show log`.
- **Moving the arms needs AUTO and RAPID running.** Homing both arms
  through the Lynx gateway on 2026-11-11 reached the controller and was
  refused with 409 "RAPID is not running": the robot was in manual
  reduced mode (`MANR`), where the controller also refuses a remote
  RAPID start. Turn the key to AUTO, `POST /robot/rapid/start`, then
  `POST /robot/rws/home`.
- **DepthAI's alignment warning** (`inputAlignTo is distorted`) in the
  log is the same as with the Python gateway.
