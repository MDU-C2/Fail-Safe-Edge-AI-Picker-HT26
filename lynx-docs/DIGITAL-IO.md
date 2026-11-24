# Lynx digital input and relay output

What the lab Lynx (`lynx-91-13-c0`, Lynx-3510-E-F2G-P8G-LV, part `3627-0510`,
WeOS 5.30) offers on its I/O terminal, how to read the input, and how an app on
the Lynx can use it as an on/off switch. Found on 2026-11-24.

## Hardware

The 4-pin I/O terminal is separate from the power terminal. Each pin has a fixed
direction; nothing is configurable:

| Pin | Marking | Direction | Description |
|---|---|---|---|
| 1 | C | Output | Relay output, common |
| 2 | NO | Output | Relay output, normally open |
| 3 | DI + | Input | Digital in + |
| 4 | DI − | Input | Digital in − |

Neither side supplies power. Both need an external voltage source.

**Digital input** (opto-isolated, SELV)

- Max 60 VDC, input current ≤ 2.9 mA at 60 VDC.
- Logic one (High): > 8 VDC. Logic zero (Low): < 5 VDC.
- Isolated from all other ports except the relay output.
- A dry contact alone does nothing: wire `supply + → switch → DI+`, `supply − → DI−`.

**Relay output**, also called the "alarm relay", "status relay" or "fault contact"
(solid-state, normally open, potential free, opto-isolated, SELV)

- Max 60 VDC / 80 mA, contact resistance max 30 Ω.
- Needs an external load in series with an external voltage source: it switches a
  circuit, it does not drive one.
- Isolated from all other ports except the digital input.
- Driven by WeOS alarm actions, not set directly. The running config already has
  alarm action `1` with targets `snmp`, `log`, `led`, `digout`.

Connectors: detachable screw/push-in terminals, 0.2–1.5 mm² (AWG 24–16), 10 mm strip.
External circuits must be SELV, galvanically isolated from mains.

## Reading the input

### CLI

```
show sensor
```

```
SENSOR           VALUE
Digital in       Low
PWR 1            On
PWR 2            Off
Temperature      37.75 C
```

### SNMP (read-only, v2c, community `public`)

WeOS publishes the input through the standard Entity and Entity Sensor MIBs
(RFC 4133 / RFC 3433). Entity index `1003` is the digital input:

| OID | Meaning | Value seen |
|---|---|---|
| `1.3.6.1.2.1.47.1.1.1.1.2.1003` | entPhysicalDescr | `Digital in` |
| `1.3.6.1.2.1.47.1.1.1.1.7.1003` | entPhysicalName | `digin1` |
| `1.3.6.1.2.1.99.1.1.1.1.1003` | entPhySensorType | `12` (truthvalue) |
| **`1.3.6.1.2.1.99.1.1.1.4.1003`** | **entPhySensorValue** | **`1` = High, `2` = Low** |
| `1.3.6.1.2.1.99.1.1.1.6.1003` | entPhySensorUnitsDisplay | `false = Low, true = High` |

The same tables hold `1001` PWR 1, `1002` PWR 2 (`1` = On, `2` = Off) and `1004`
temperature (°C). Westermo's private MIB (`1.3.6.1.4.1.16177`) has no digital-in object.

## Using it in a Lynx app

Apps run in the `host-os` network namespace, so a container can query the host's
SNMP agent at `127.0.0.1:161` (or `169.254.219.127:161`) with no app-hosting
changes. A device file or GPIO is not reachable: app-hosting can share only ports,
veth pairs and external media.

As an on/off switch:

1. Wire a 12–24 VDC supply through a switch into DI+ / DI−.
2. In the app, poll `entPhySensorValue.1003` a few times a second with SNMP GET.
3. Fail safe: treat only `1` (High) as "run". Low, a timeout or a bad reply mean
   "stop", so a broken wire or missing supply stops the app.

Not verified yet:

- How fast the SNMP value follows the input (WeOS may sample about once a second).
  Fine for a switch, not for fast signals.
- A query from inside a running container (tested from the laptop only).

Alternative: a WeOS alarm trigger on the digital-in sensor can send an SNMP trap
that the app receives on UDP 162. It reacts faster but changes the configuration
of the shared switch.

## Sources

- Westermo, *Lynx 3000 Series user guide* 6627-2210 rev H (covers the Lynx 3510):
  §3.4.2 I/O connection and the interface specifications.
  <https://www.westermo.com/-/media/Files/User-guides/westermo_ug_6627-2210_lynx-3000_revh.pdf>
- Lynx-3510-E-F2G-P8G-LV datasheet: 1 digital input, 1 digital output, Entity Sensor MIB support.
- Live device: `show sensor`, `show app-hosting` and an SNMP walk on 2026-11-24.
- The older *Lynx Series user manual* 6640-2001 (Lynx 3640, 2006) on ManualsLib shows
  the same idea (Digital in ± and Alarm relay/Status ±) but a different model; do not use
  its numbers.
