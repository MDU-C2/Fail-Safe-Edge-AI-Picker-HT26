# WeOS CLI command reference (Westermo Lynx)

Captured live from `lynx-91-13-c0` (WeOS v5.30.0) on 2026-11-11 using the device's
own `help`, `help all`, `tree`, `tutorial` and `show` commands. The complete node
tree (EXEC + every config context) is in [command-tree.txt](command-tree.txt); this
file explains the model and documents the commands you actually use.

> A sanitized snapshot of the device's live config (`running-config.sanitized.json`)
> is kept **local only** — it is git-ignored and **not exposed in the repo for
> security reasons**. Regenerate it any time with `show running-config` over SSH.

---

## 1. How the CLI works

WeOS has **two modes**:

- **EXEC mode** (prompt `host:/#>`) — status, diagnostics, file ops, reboot/upgrade.
  This is where `show`, `ping`, `copy`, `app-hosting`, etc. live.
- **Configuration mode** — a *tree of contexts* you enter with `configure`. You
  "cd" **into** a context by typing its name (e.g. `port eth3`, `iface vlan1`,
  `management ssh`) and back **out** with `end`/`leave`/`abort`. There is no literal
  `cd` command — navigating the context tree *is* the `cd`.

### Navigation & editing (works in both modes)

| Key / word | Action |
|---|---|
| `help` / `help all` | List commands (compact / full) in the current mode |
| `help <cmd>` | Detailed syntax + usage for one command |
| `tree` | Print the whole command/context tree from here down |
| `where` | Show your current position in the context tree |
| `show` | Summary/status of the current context |
| `<TAB>` | Complete commands/arguments; shows choices |
| short forms | Any unambiguous prefix works (`sh ru` = `show running-config`) |
| `do <cmd>` | Run an EXEC command (e.g. `do ping …`) without leaving config mode |
| `no <cmd>` | Disable/remove a setting or service |
| `repeat <cmd>` | Re-run a command every second until `Ctrl-C` |
| `end` | Save settings, go up one context (to parent / EXEC) |
| `leave` / `Ctrl-z` | Save settings and return all the way to EXEC |
| `abort` | Discard changes made in this context and leave it |
| `Ctrl-d` | Abort current context / logout at top level |
| `Ctrl-c` | Abort the current command |

Config changes take effect only when you `leave`/`end`/`Ctrl-z`. They are **not
persistent** until you save (`copy running-config startup-config`, see §6).

### EXEC vs config meaning of `show`

Many `show` commands change meaning by mode: in EXEC they show **live status**, in
config mode they show the **pending configuration**. `show ports`, `show vlans`,
`show ifaces` are the examples the built-in `tutorial` calls out.

---

## 2. Device & system status (read-only, safe)

```sh
show system-information     # product, serial, firmware, boot banks, cards
version                     # running firmware build
uptime                      # load + how long up
show memory                 # RAM usage
show processes              # running processes
show sensor                 # temperature, power, digital-in, SFP DDM
status                      # list of active warnings/errors
whoami  /  id  /  users     # who you are / unix ids / who's logged in
show history                # command history
system-information          # (same as 'show system-information')
date [YYYY-MM-DD hh:mm[:ss]] # show/set system clock (local time, see DEPLOY.md)
config-hash                 # SHA256 of the running config
```

## 3. Interfaces, ports, VLANs, L2 (read-only forms)

WeOS layers **port → VLAN → interface** (see `tutorial`). A `port` is the physical
jack; a `vlan` groups ports; an `iface` (usually mapped to one VLAN) holds the IP.

```sh
show ports                  # physical port link/speed/state/VLAN
show port eth3 detailed     # one port in detail
show vlan [VID]             # 802.1Q VLAN membership (tagged/untagged)
show ifaces                 # interfaces + IP addresses + source (this is the IP view)
show iface vlan1            # one interface
show fdb                    # MAC forwarding database (a.k.a. ATU/macs)
show lldp                   # LLDP/CDP neighbours
show spanning-tree          # RSTP status
show poe [full]             # Power-over-Ethernet status
show lag / show aggregates  # link aggregation
show ring / frnt / mrp / coupling   # Westermo ring-redundancy protocols
```

On this unit: one VLAN `vlan1` with **all** ports untagged; `vlan1` carries the mgmt
IP. Up ports as seen 2026-11-11: eth3, eth4, eth8, eth9, eth10.

## 4. IP / routing / L3 (read-only forms)

```sh
show ip route               # unicast routing table
show ip                     # IP settings summary
show arp                    # ARP cache
show ip firewall            # firewall rules/counters/conntrack (firewall is OFF here)
show ip hosts               # local host table
show ipv6 / show ipv6 route # IPv6 (disabled on this unit)
show dhcp-clients           # active DHCP server leases (if DHCP server on)
show ntp                    # NTP client/peer status
```

## 5. Diagnostics / connectivity tools

```sh
ping [iface IF] [count N] [size B] [ttl N] <HOST>   # runs forever w/o 'count'
traceroute <HOST>
nslookup <HOST> [SERVER]
tcpdump <..>                # packet capture on an interface
ssh [user U] [force ipv4|ipv6] HOST[:PORT]          # SSH from the switch to another host
telnet <HOST>
ipcalc <..>                 # subnet calculator
ledflash [s]                # blink all yellow port LEDs (physically locate the unit)
```

`ping` example: `ping count 3 169.254.1.223` (the OAK camera).
For an IPv6 link-local target you must give the interface: `ssh admin@fe80::1%vlan1`.

## 6. Configuration files & saving

WeOS keeps named config files; `startup-config` is a *pointer* to one of them.

```sh
show running-config         # live config, RAM only (JSON) — NOT yet saved
show startup-config         # config loaded at boot (from flash/USB/SD)
show factory-config         # read-only factory defaults
show safe-config / no-config
diff <FILE1> <FILE2>        # unified diff of two config files

copy running-config startup-config   # *** SAVE current config to survive reboot ***
copy running-config config1          # save to a named file
copy console config1                 # paste a whole config file in via the terminal
copy startup-config <proto>://...    # back the config up off-box
config-hash                          # verify a config by hash
```

`help copy` details the WeOS VFS and the "paste area" (`copy console FILE`).
File-level ops also live under the `file` context: `file cfg dir|copy|erase|import|export`,
`file log dir|show|export`, `file app dir|import|erase`.

## 7. Entering configuration mode (making changes)

```sh
configure                   # enter config mode (interactive, with yes/no safeguards)
configure terminal          # same, but safeguards OFF (for pasting whole configs)
```

Then "cd" into a context by naming it, set values, and `leave` to apply. Examples:

```sh
configure
  system
    hostname lynx-lab-1                 # rename the device
    leave
# --- change a port ---
configure
  port eth5
    disable                             # 'no disable' / 'enable' to turn back on
    description "spare"
    speed-duplex 100-full
    leave
# --- give the switch a static management IP on vlan1 ---
configure
  iface vlan1
    inet
      no dhcp                           # (navigate/adjust address source)
    leave
# --- management services ---
configure
  management
    ssh      port 22   enable
    telnet   no enable                  # turn Telnet off (recommended)
    web http no enable                  # HTTPS-only
    leave
```

Use `tree` inside any context to see everything settable below it, or consult
[command-tree.txt](command-tree.txt). Top-level config contexts include:
`aaa`, `alarm`, `audit`, `port(s)`, `vlan(s)`, `vrf`, `iface`, `ip`, `ipv6`, `fdb`,
`ring/frnt/mrp/coupling`, `router` (ospf/rip/pim/vrrp), `spanning-tree`, `ntp`, `ptp`,
`system`, `logging`, `monitor`, `dhcp-server`, `dhcp-relay`, `tftp-server`, `dns`,
`tunnel` (gre/ssl/ipsec/wireguard), `app-hosting`, `discover` (mdns/ssdp),
`management` (web/ssh/telnet/snmp/cli/console), `lldp`, `profinet`, `metrics`, `poe`,
`watchdog`, `port-access` (802.1X/MAC-auth), `pki`, `policy`.

## 8. App hosting (containers on the switch) — project-relevant

The Lynx runs the fail-safe **Lynx-tier model** as a WeOS app. `app-hosting` (EXEC)
manages running apps; `configure → app-hosting` defines them; `file app` manages the
`.wmopkg` images. The full deployment walkthrough (build, import, define, start,
update, script it) is [DEPLOY.md](DEPLOY.md). An app's output is in
`show log app-<app>.log`; `app-hosting log <app>` may show nothing.

```sh
# status
show app-hosting                 # apps + imported images + storage media/path

# image management
file app dir                     # list imported app images
file app import <proto>://.../lynx-picker-0.1.1.wmopkg   # install an image
file app erase <name>
app-hosting image                # show imported images
app-hosting label <image> <lbl>  # (re)label an image so an app can reference it

# run / observe an app  (app ids on this unit: lynxpicker, yolotest, gateway,
#                         yololivetest, yolowebtest)
app-hosting start   lynxpicker
app-hosting stop    lynxpicker
app-hosting restart lynxpicker
app-hosting reload  [label]      # update apps whose image matches <label>, restart
app-hosting attach  lynxpicker   # drop into the app's shell (exit / Ctrl-d to detach)
app-hosting app     lynxpicker   # show one app's config/detail
app-hosting log     lynxpicker   # dump the app's log
app-hosting follow  lynxpicker   # tail -F the app's log

# define/change an app
configure
  app-hosting
    app lynxpicker
      image lynxpicker                       # image label to run
      enable
      autostart                              # start on boot ('no autostart' to disable)
      cmd "/bin/sleep infinity"
      environment GATEWAY_CAMERA_IP 169.254.1.223
      share path external/app/images /images # host path -> guest mount
      leave
    storage media external                   # where images live
    cpu 50                                    # % CPU shared by all apps
    ram 50                                    # % RAM shared by all apps
    leave
```

On this unit images are on **external** media under `app/images`; the `lynxpicker`
app mounts `external/app/lynx-picker/state → /state` and `external/app/images →
/images`, sets `GATEWAY_CAMERA_IP=169.254.1.223`, and currently runs
`/bin/sleep infinity` (idle placeholder). Build images as `.wmopkg` for
`linux/arm64` — the gateway already has a generator:
[gateway/docker/generate-container.py](../gateway/docker/generate-container.py).

## 9. Logging, audit, monitoring

```sh
show logging [all|internal]      show logfile <FILE>      follow <FILE> (tail -F)
audit show | status | last N | search STR | category C    # audit ring buffer
show alarm [log]                 # alarm status / log
show metrics [cpu|memory|port]-utilization
show monitor                     # port mirroring setup
rmon statistics                  # per-port counters
```

## 10. Firmware & lifecycle (DANGEROUS on shared hardware)

```sh
firmware-checksum                # CRC-check the firmware banks
upgrade [validate] <all|pri|sec|boot> [proto ..] <addr> FILE   # flash new WeOS .pkg
reboot [cold]                    # reboot from startup-config (cold = watchdog reset)
factory-reset [no-reboot]        # WIPE everything incl. IP + password
logout / quit                    # leave the CLI
```

`upgrade` uses Westermo `.pkg` bundles; `validate` dry-runs without installing;
`force` skips the signature check (**can brick the device** — last resort only).

## 11. Hardening notes (this unit ships wide open)

Observed on 2026-11-11, worth fixing before the switch leaves the bench:

- Login is factory **admin/admin** → set a real password:
  `configure → aaa → user admin → password` (or `system → username`).
- **Telnet (23)** and **HTTP (80)** are enabled → disable, keep SSH + HTTPS:
  `configure → management → telnet no enable`, `… web http no enable`.
- **SNMP** is on with community `public` → disable or set v3:
  `configure → management → snmp …`.
- No static mgmt IP (DHCP + link-local only); the firewall is **off**.
- After any change: `copy running-config startup-config` to persist.

---

### Command index

The authoritative, exhaustive list of every EXEC command and config node — with
one-line descriptions — is [command-tree.txt](command-tree.txt) (generated by the
device's own `tree` command). Use `help <command>` on the device for full syntax of
any single command.

A sanitized dump of the live device config is available locally as
`running-config.sanitized.json` (git-ignored, not committed — see the note at the top
of this file); regenerate it with `show running-config` over SSH.
