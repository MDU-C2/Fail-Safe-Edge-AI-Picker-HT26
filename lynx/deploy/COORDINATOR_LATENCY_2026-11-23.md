# Coordinator latency and device switching — 2026-11-23

The MacBook and Pi repeatedly exchanged control without submitting another cup. The Pi completed fresh scans, then yielded and reported that it no longer owned control. The gateway still held the earlier completed cup; the new attempts were not submitted.

An isolated benchmark on Lynx reproduced the underlying failure using a temporary copy of the saved coordinator history and a fake gateway. Preparing a receipt took **3.44 seconds**, while the installed client timed out after **3.02 seconds** against its three-second deadline. The coordinator finished persisting a pending receipt after the client had already given up. This caused cleanup, lease release, and another device's attempt rather than physical picking.

The tested history contained 64 submissions and 13 runs in 908,894 bytes. Reading/decoding it took 0.17–0.22 seconds, full validation took 0.43–0.54 seconds, and durable whole-file commits took 2.46–2.70 seconds. These operations also held the shared lock, delaying otherwise small status requests.

## Relationship to the speed fields

The saved pre-speed and speed-update versions show no change to polling intervals, coordinator locking/persistence, queue preparation, or the three-second deadline. The update added validation of three speed numbers, a capability flag, and one status request when the user explicitly applies speeds. Continuous batch admission already made its capability request. Displaying the fields reads cached values; it does not add gateway polling.

A speed map occupies 71 compact JSON bytes per run. The observed timing after that update does not establish that these fields alone caused the threshold crossing. Testing before deployment did not cover the accumulated history and measured Lynx storage latency. The fix retains the separate speed fields and addresses the demonstrated coordinator bottleneck.

## Changes

- Cache validated public coordinator status against a digest of the exact stored bytes. Unchanged data reuses validated results; changed bytes trigger full validation. File/process locks and complete historical validation remain in force. No history is discarded or trusted solely from file timestamps.
- Increase the default coordinator metadata deadline to 15 seconds, including status requests that may wait behind a durable commit. TCP connection establishment remains bounded to one second. A possibly committed metadata operation is still not blindly retried inside the HTTP client.
- Preserve a confirmed, valid lease while retrying a read-only transient failure at an appropriate boundary. Ownership loss, unconfirmed lease health, Stop, and physical submission uncertainty continue through their existing reconciliation paths. Temporary status failures no longer needlessly trigger another device takeover.
- Report the transient reason in workflow status so a future timeout is distinguishable from a camera or ownership problem.

Robot movement plans, per-device speed settings, physical command submission rules, and continuous-picking behavior are unchanged.

## Verification and installation

Targeted test commands passed **409**, **233**, and **158** tests respectively. These commands overlap; the figures must not be added into a distinct-test total. Tests cover cached-state validation, mutations and corruption, durable metadata operations, ownership handling, camera recovery, and existing workflow behavior.

The final Lynx stress check used the same full history with three roles polling concurrently. All 20 status/heartbeat requests succeeded without timeouts (median 0.093 seconds, maximum 3.826 seconds); receipt preparation completed once in 3.338 seconds and rejection once in 3.019 seconds. It used temporary history and a fake gateway, sent no robot commands, and preserved the live state. This does not constitute a physical disconnect-and-takeover test.

The Pi update is installed and armed (PID 34164). The Lynx combined coordinator and standby update is installed (PID 19042); read-only verification confirmed both final source hashes, the expected manifest, all 213 protected fingerprints, the unchanged stopped run and receipt, the completed 19-step gateway queue, idle control, and both completed arm statuses. All three workers reported fresh armed heartbeats. Lynx's initial installer readiness wait expired while its existing 128 MiB memory-headroom guard delayed model startup. After the installer exited, the existing retry loop passed the unchanged guard and armed the native worker automatically; no additional restart or guard reduction was needed.

The MacBook UI has been reloaded (PID 51400), and final cross-device verification passed. All three armed workers' heartbeats advanced during observation, the UI was ready and idle with no error or recovery requirement, continuous mode remained enabled, and the device speeds remained 1000/700/400. Sequential live coordinator reads took 13–31 milliseconds (median 17 milliseconds). The stopped run, receipt, and completed gateway queue remained unchanged. The Mac clock was 26–27 seconds ahead of Lynx, so remote heartbeat freshness was checked against the coordinator's HTTP Date and observed timestamp advancement; no clocks were changed.

The affected shared run was stopped at the admission/coordinator level to prevent further attempts while preparing the repair; no robot Stop was sent for that action. Its accepted queue and saved receipts remain preserved. No physical pick or disconnect-and-takeover test was initiated during installation or verification.

Evidence is in workspace `tmp/picker-churn-fix-20261123/`, including `lynx-http-old-deadline-benchmark.json`, `lynx-validation-benchmark.json`, `lynx-admission-benchmark.json`, `lynx-http-final-stress.json`, `lynx-deployment-result.json`, and `final-live-verification.json`.
