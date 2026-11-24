# Camera recovery during device takeover — 2026-11-23

The Raspberry Pi acquired control after the MacBook disconnected, but a subsequent camera depth request exhausted its retry window with HTTP 503. The camera client then raised a generic camera error, losing the transient error type. The shared workflow treated that error as a permanent batch failure, so the Pi appeared not to take over.

Live inspection also found 63 established connections against the gateway's 64-connection limit. The idle Pi worker retained twelve gateway connections across repeated observations. This explains the gateway's connection pressure; the exact Python object retaining each socket was not determined. There was one Pi picker worker, not a duplicate picker process.

## Changes

- Camera capture exhaustion preserves its transient error type and cause. A temporary camera outage yields and takes a new observation while keeping the shared batch active.
- Temporary gateway status reads and control acquire/renew/release failures have explicit retryable types. The runner waits with bounded exponential backoff and resumes through its existing queue-reconciliation and ownership checks.
- Physical queue, move, Home, gripper and Stop requests retain their single-attempt behavior. An uncertain physical submission is reconciled through its saved identity and completion evidence; it is never blindly replayed because an HTTP response failed.
- Gateway-facing camera, robot, queue, coordinator and active-device status requests use `Connection: close`, releasing gateway connection capacity after each response. This does not change the gateway server.
- Authentication refusals, malformed status, failed movement, and unconfirmed completion remain errors requiring the appropriate recovery. Temporary read failures do not clear queues or send robot Stop.

Accepted cups still finish their full queue before another device acts on the next cup. Returning higher-priority devices use the same shared execution and fresh observations. Existing handover geometry, arm parking, per-device speed settings, measured Home barriers, grid refresh and continuous-picking behavior are unchanged.

## Verification

The broad offline run had 849 passing tests and 265 passing subtests, plus one stale test expectation that omitted the already-supported device-speed capability. After correcting that expectation, all 16 tests in its startup-test file passed. Integration regressions exercise actual camera-client HTTP 503 exhaustion before the first pick and between completed cups, Pi takeover after a primary cup finishes, fresh recapture, temporary read/control failures, permanent refusals, and a returning primary observing completion without replay.

The deployment verifier uses temporary journals and a fake gateway, blocks real network and subprocess access, and checks two distinct cup queues followed by exactly two final Home queues. Candidate imports, saved-receipt compatibility and unchanged motion fingerprints are checked before installation. These are offline checks; no physical disconnect-and-takeover test has been performed for this update.

## Installation status

- **Pi installed:** `failsafe-picker-armed.service` restarted as PID `33441` and reported a fresh armed heartbeat. The candidate takeover verifier passed. Protected pending/recovery records, calibration, grid and speed files remained unchanged, as did unrelated source files and the gateway queue.
- **Lynx installed:** combined coordinator/standby service PID `14733`, with native model PID `14899`. All six installed source fingerprints matched the candidate, all 209 protected fingerprints remained unchanged, and all three workers reported fresh armed heartbeats. Model startup took approximately 64 seconds because its memory guard waited for sufficient headroom; the guard was preserved. The initial installer readiness timeout was followed by successful full verification, without another restart.
- **MacBook UI installed:** restarted as PID `48985` and reported ready, idle, with no error or recovery requirement. Continuous picking, three separate device speeds, pending receipts, calibration, grid and handover source remained unchanged.

After installation, three fresh 1920 × 1080 RGB/depth pairs were captured on the Pi in 0.223, 0.219 and 0.331 seconds, with distinct frame sequences and no HTTP 503. The shared run and gateway queue were unchanged. The gateway's established connection count fell from 63 to 51, leaving 13 slots; the current Pi, Lynx and MacBook workers held no idle gateway sockets at inspection. A second inspection after those captures still found 51 connections, with the same per-device counts. Historical connections from earlier clients remained; no gateway restart or configuration change was performed.

The installer did not start a batch or send robot movement. It preserved the existing blocked run `6d9390d57d9c40399cc4956cae0333b5` and its non-submitted Pi receipt. This historical blocked run is not automatically reopened: use **Scan**, then **Pick all**, to explicitly start the next batch after installation is complete.

Local evidence is in workspace `tmp/pi-takeover-fix-20261123/`. The Pi deployment result is `/home/kasun/picker-devices-20261113/takeover-update-20261123/deployment-result.json`; its source backups, protected-state hashes, camera check and offline verifier report are alongside it. Lynx's corresponding result and backups are in `/state/cli-0.2.0/takeover-transport-update-20261123/`. Local `mac-deployment-result.json` records the UI reload and preserved settings.
