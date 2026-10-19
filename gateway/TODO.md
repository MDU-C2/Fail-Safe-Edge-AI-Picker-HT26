# Gateway TODO

## Open for discussion

- **Nothing happens when the control lease lapses.** The lease is only checked when it is next read; there is no timer. If the controlling client crashes or loses the network, its lease just expires: a move already sent over RWS (`/robot/rws/move`, `/robot/rws/home`) or the legacy socket runs to the end, RAPID keeps running, and no `control.*` event is published. For a fail-safe system the gateway should notice the lapse itself (a background watchdog) and react. What it should do is not decided yet, for example:
  - stop RAPID (`POST /rw/rapid/execution?action=stop`), which halts the arm mid-move;
  - let the current move finish but refuse new ones until someone acquires control;
  - move to a safe pose (home) first, then stop;
  - only publish a `control.expired` event and leave the reaction to the next controller (cloud → Pi → Lynx fallback).

  It also needs deciding whether a preemption by a higher-priority client should interrupt a move in progress.

## Planned

- Pin the versions in `requirements.txt` so every Docker build installs the same packages.
- Keep `gateway/.env`, `gateway/auth_clients.json` and the calibration file outside the container: mount them from the host (for example `-v` on the Pi) instead of baking `.env` and `auth_clients.json` into the image, so config and tokens can change without a rebuild and the calibration survives a redeploy.
- RWS endpoints (`gateway/rws/`): the gateway does not watch the controller, so `robot.rapid.*` events only fire for actions taken through the gateway (see the lease item above for what a lapse does). `/robot/status` still comes from the TCP robot service, not RWS.
- RWS moves (`/robot/rws/*`): the result is polled every 50 ms rather than pushed with an RWS subscription; after a 504 timeout the move carries on in RAPID and new jobs for that arm get 409 until it finishes. The default safe zone (±1000 mm, copied from CommServer) does not protect anything yet; set `GATEWAY_RWS_SAFE_ZONE_MIN/MAX` to tested points (possibly one box per arm). A failed move only reports `ERR move failed` (the arm modules have no error text).
