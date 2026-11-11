# Deploying an app to the Lynx

How to get any container app onto the Lynx (WeOS 5.30 app hosting) and
run it, as worked out and tested on 2026-11-11 by deploying the C++
robot gateway. The gateway's own steps, script and results are in
[gateway/cpp/LYNX.md](../gateway/cpp/LYNX.md); use it as the worked
example. CLI basics (EXEC vs configuration mode, `leave`, `show`) are in
[COMMANDS.md](COMMANDS.md).

> Shared lab hardware: importing an image and adding an app touch
> nothing else, but removing or changing someone else's app, saving the
> config, or rebooting affects everyone. Agree on it first.

## The model

| Thing | What it is | Commands |
|---|---|---|
| **Image** | a `.wmopkg` file: the app's root filesystem as an xz squashfs inside a FIT image (optionally signed). Stored on the configured media (here `external`, path `app/images`) | `file app import`, `show app-hosting`, `app-hosting image` |
| **Label** | a name (≤ 15 characters) pointing at one image; apps refer to labels, so a new version is a label move | `app-hosting label <image> <label>`, `app-hosting reload <label>` |
| **App** | configuration: which label, the command, network namespace, shares, environment, capabilities, autostart. **At most 5 apps** | `configure` → `app-hosting` → `app <name>` |
| **Running app** | an OCI container (crun + conmon) | `app-hosting start/stop/restart <app>` |

All apps together get the `cpu` and `ram` percentages of
`configure → app-hosting` (here 50 % / 50 %, i.e. ~248 MB of the
Lynx's 496 MB).

## 1. Build the image

The Lynx is **arm64** (`linux/arm64`). The image needs no shell or init
system: WeOS runs one command in it.

Westermo's [gateway/docker/generate-container.py](../gateway/docker/generate-container.py)
makes the `.wmopkg` (Linux/WSL; needs `docker buildx`, `mksquashfs`
from squashfs-tools, `mkimage` from u-boot-tools, `dtc`, `file`):

```sh
# from a Dockerfile (builds it with its own buildx builder):
python gateway/docker/generate-container.py -i <Dockerfile> -t docker -n <name> -v <version> \
    -d "<description>" -a linux/arm64 [-k <signing key>]

# from an image you already built (keeps your Docker build cache):
docker build --platform linux/arm64 -t <name> .
docker export $(docker create --platform linux/arm64 <name>) > rootfs.tar
python gateway/docker/generate-container.py -i rootfs.tar -t rootfs -n <name> -v <version> \
    -d "<description>" -a linux/arm64
```

Output: `<name>-<version>.wmopkg` in the current directory; the image is
then called `<name>-<version>` on the Lynx. `gateway/cpp/dev.sh wmopkg`
is the second form for the gateway, with the tools in a container.

Keep in mind:

- **Only the filesystem goes into the package**: `ENTRYPOINT`, `CMD`,
  `ENV`, `EXPOSE`, `WORKDIR` of the Dockerfile are dropped. Set the
  command (with its arguments) and environment on the app instead.
  WeOS's default command is `/sbin/init`.
- **The image is read-only** at run time. Anything the app writes goes
  to a share (below).
- **Size**: uploads of ~100 MB or more fail in the web UI. The CLI
  import below has only been used with a 12 MB package; RAM and flash
  are small anyway.

## 2. Import it

The Lynx downloads the file itself; scp to it is off
(`management ssh external-access`). Serve the file from the laptop's
address on the lab network and import it (EXEC mode):

```powershell
# laptop (169.254.125.10 on "Ethernet 2"), in the folder with the .wmopkg:
python -m http.server 8099 --bind 169.254.125.10
```

```
file app import proto http 169.254.125.10:8099 /<name>-<version>.wmopkg
app-hosting label <name>-<version> <label>
show app-hosting
```

Other sources work the same way: `proto ftp|tftp|scp|sftp|https`, with
`user`/`password` where needed (see `help file app import`). An unsigned
package logs "File signature check failed. Reason: Signature Error" and
is still installed (`VALID Yes, VERIFIED No` in `show app-hosting`).
Importing the same name and version again replaces it.

## 3. Define the app

```
configure
app-hosting
app <app>
image <label>
cmd "<command with arguments>"
netns host-os                                   # or user-ns (own namespace, the default)
share media external app/<app>/state as /state create
environment <NAME> <value>                      # one line per variable
capability <CAP_...>                            # only if needed
enable
no autostart                                    # or: autostart
leave
```

- **`netns host-os`** gives the app the Lynx's own interfaces and
  addresses (it can reach the OAK camera on `169.254.1.223` and is
  reached at the Lynx's address), but it cannot reserve ports. With the
  default `user-ns` the app gets its own namespace (and can reserve
  ports); getting traffic in and out then needs ports or veth pairs
  shared into it (`help share`). Only `host-os` has been tried here.
- **Shares** map a directory on the Lynx's media into the app.
  `create` makes the directory if missing; without it and without the
  directory the app shows **"Dependency fail"** and does not start.
- `leave` applies the configuration; it is **not persistent** until
  `copy running-config startup-config`, which saves the whole running
  config (everyone's unsaved changes too).
- A sixth app is refused, and only `show log` tells you ("Max number of
  apps reached"). Free a slot with `no app <name>` (removes the
  definition only; images and shared data stay).

## 4. Start, watch, stop

```
app-hosting start <app>
show app-hosting                   # Running / Stopped / Created / No image / Dependency fail
app-hosting app <app>              # the app's configuration
show log app-<app>.log             # the app's stdout/stderr (`app-hosting log` may show nothing)
follow app-<app>.log               # tail it (Ctrl-C)
show processes                     # CPU and VSZ of the app's process; `show memory` for the device
app-hosting attach <app>           # shell in the app (only if the image has the configured `shell`)
app-hosting stop <app>
app-hosting restart <app>
```

## 5. Update

Build and import the new version, then move the label and reload:

```
file app import proto http 169.254.125.10:8099 /<name>-<new>.wmopkg
app-hosting label <name>-<new> <label>
app-hosting reload <label>         # restarts the apps using that label
```

Old images stay on the media: `file app erase <image>` does not find
images on external media on WeOS 5.30 ("Path does not exist").

## 6. Scripting it

The CLI reads commands from an SSH session's standard input, so a
deployment can be scripted from the laptop with PuTTY's `plink`:

```powershell
# one EXEC command
plink -ssh -batch -hostkey <key> -pw <password> admin@169.254.219.127 "show app-hosting"

# several lines (configuration mode): feed them on stdin, start with an empty line
"`nconfigure`napp-hosting`napp <app>`nenvironment FOO bar`nleave`nlogout`n" |
    plink -ssh -batch -T -hostkey <key> -pw <password> admin@169.254.219.127
```

Windows PowerShell can put a byte-order mark in front of piped text,
which makes the first line "not found"; the leading empty line absorbs
it. Errors in such a session are not returned; check `show log`.
[gateway/cpp/tools/deploy_lynx.ps1](../gateway/cpp/tools/deploy_lynx.ps1)
does steps 2–4 this way for the gateway.

## Lynx specifics worth knowing

- The clock starts at **1970** (no time source on the lab network), so apps
  see a wrong wall-clock time until it is set. Set it in local time (the
  timezone is `Europe/Stockholm`, set with `configure` → `system` →
  `timezone Europe/Stockholm` → `leave` and saved 2026-11-11) with the EXEC
  command `date YYYY-MM-DD hh:mm:ss`, e.g. from the laptop:

  ```powershell
  $t = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  plink -ssh -batch -pw admin admin@169.254.219.127 "date $t"
  ```

  It changes no configuration, but is most likely lost on a reboot (set
  2026-11-11); an NTP server on the lab network would make it stick.
- The Lynx has two addresses on `vlan1`: `169.254.219.127/16`
  (link-local, DHCP gets no answer) and `192.168.125.20/24` (static,
  added and saved 2026-11-11). The YuMi only
  answers on `192.168.125.1`, so an app with `netns host-os` reaches it
  through the second one.
- The OAK camera can be held by one process at a time: stop other
  camera apps (and the gateway on the Raspberry Pi) first.
