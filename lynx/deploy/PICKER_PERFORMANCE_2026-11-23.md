# Pi takeover and between-cup latency — 2026-11-23

## Findings

The observed Pi takeover took about 29 seconds from its first acquisition to
the first queued cup. Its log included gateway connection resets, release and
reacquisition. Model inference took 1.82–2.18 seconds. The slow attempt was not
a fixed 30-second failover timer or a 30-second inference operation. Healthy
attempts took about four seconds to scan, followed by three to five seconds
before queue callbacks with the old coordinator writer.

The gateway retained 60 of its 64 connection slots, including 13 old Pi
connections and 33 Mac connections. Neither client had an established gateway
connection during the matching client-side inspection. The retained sockets
had empty transmit/receive queues and no retransmission timer. The existing
five-second `SO_RCVTIMEO` does not bound synchronous Boost.Asio reads: its
retry path waits indefinitely after the socket returns `EAGAIN`. An isolated
loopback test reproduced this behavior. Lynx's standby worker uses the local
gateway path and had no retained connections in this sample.

Mac history showed camera checks around 2.17–2.87 seconds, then another
2.5–4 seconds to prepare the next cup. The coordinator had accumulated 74
submission receipts across 14 runs, totaling 1,062,905 bytes as formatted JSON.
Saving this data took about 3.13 seconds on Lynx. Most time was JSON formatting
and serialization; file synchronization itself took under a millisecond in the
measured samples. These receipts preserve authorization and the completion of
previous queues so takeover does not repeat a cup.

## Installed Python changes

The coordinator now serializes once to compact JSON and validates those exact
bytes before writing. It still creates a private temporary file, synchronizes
the file, atomically replaces the destination and synchronizes its directory
before acknowledging the commit. Existing formatted files remain readable.
No receipt or completion evidence is removed. The same state occupies 480,127
compact bytes. Three isolated durable commits on Lynx took 0.572, 0.572 and
0.641 seconds; these used a copy of the live history and no robot commands.
A subsequent benchmark of the installed complete `prepare_queue` operation
against all 74 saved receipts took 0.759 seconds; resolving a known rejected
submission took 1.128 seconds. All network calls were blocked and the gateway
was a test double; the live state hash remained unchanged.

Fresh camera reads now wait for the small camera-status sequence to advance
before opening RGB/depth streams. Previously the client could repeatedly open
large image streams and discard stale pairs while waiting for the next frame.
The initial freshness baseline, capture deadline, matching RGB/depth sequence,
dimensions, timestamps and payload checks remain in place. An offline 4 Hz
five-frame simulation reduced image requests from 62 to 10 while returning the
same five sequences. This reduces transfer pressure; it does not increase the
camera frame rate or guarantee the same reduction in elapsed scan time.

The existing five depth samples, one observation, latest raw aligned grid
evidence, workspace/grid refresh, motion plans, cup completion barriers,
continuous picking, priority reclaim and device speed settings are preserved.
The latest-frame legacy capture path is unchanged.

Both updated Python files are installed on Mac, Pi and Lynx. Pi's armed user
service and Lynx's combined coordinator/standby service were restarted only
after verifying the stopped shared run and completed gateway queue. Mac's UI
was reloaded with its existing launch configuration. The deployed file hashes:

| File | SHA-256 |
| --- | --- |
| `camera_client.py` | `747ca0632a4ba71a8d1d2a37f732f1ca7bc8de402ace013538d4fa091d7fb77d` |
| `failover_coordinator.py` | `6d632c084ccb98764f6e77f794804ba51f3ac585744c85b7ee801dae936fc3b1` |

Cross-device checks confirmed advancing armed heartbeats on all three devices,
an idle ready UI, no recovery requirement, continuous mode enabled and speeds
1000/700/400. Shared run, receipt, completed 19-step queue, inactive control and
completed arm statuses were unchanged. Coordinator reads had a median of about
22 milliseconds. These checks preceded a single camera-only scan.

That first scan after UI reload completed in 3.718 seconds with seven cup
candidates and no UI error, while the gateway build also used the Mac. Its
five consecutive camera frames span 1.002 seconds; the application detection stage
took 2.470 seconds, including a 0.457-second provider prediction. Earlier
warm application detection timings were 0.565–0.706 seconds. The remaining
first-scan processing includes inference audit persistence, masks, geometry
and annotation; the saved timing does not separate those costs. The camera
burst itself was not slower. A subsequent
gateway status read was reset, so the
post-scan gateway comparison did not complete; the stopped shared run and head
were still unchanged. This measurement does not establish a scan-time speedup
and was recorded before the gateway transport fix below. No physical pick
or disconnect-and-takeover test was started.

## Gateway transport update

The checked-out gateway is 0.5.0, while Lynx runs 0.6.0 with queued commands.
Do not deploy this checkout's older gateway to fix the connections. An isolated
0.6.0 context is pinned to commit
`a934260617a711b2783e1b4045a0ecd3746880db`. Its 105 original files were verified
against repository blob hashes; its OpenAPI file matches the installed API
byte-for-byte. Saved primary, secondary, tertiary and diagnostics credentials,
plus the established operator credential, match its authentication registry.
The installed binary and embedded authentication file hashes were not available,
so this is not a claim of byte-identical deployed source provenance.

The candidate changes only HTTP transport and transport tests. A nonblocking
socket adapter enforces the existing idle deadline during request reads and
response writes. It leaves handler execution outside that deadline, so a robot
operation is not stopped after five seconds. WebSocket handoff restores its
previous blocking mode. Motion, queue, authentication and lease behavior are
unchanged. The pinned queue implementation and queue tests are preserved.

Ten focused restart/receipt checks passed, including six new regressions for
a reopened coordinator with an idle gateway, all three owners, stopped runs
and fresh-cup continuation without replay.

Eight isolated transport tests pass, also under AddressSanitizer and
UndefinedBehaviorSanitizer with leak detection. They cover abandoned and
partial requests, progressing traffic, stalled writes, long handlers and
WebSocket mode restoration. The full ARM64 runtime built successfully with
143 C++ tests reporting zero failures; the registry test skipped by the build
stage was rerun with the pinned registry mounted and passed. An isolated real
server with mock camera/robot and no external network reclaimed all 64 idle
connections in 5.341 seconds and admitted 64 new ones. Its compiled OpenAPI
hash matches the installed gateway. The unique package
`gateway-cpp-0.6.0-idlefix-20261123.wmopkg` is built and imported, with SHA-256
`e36ea6cdee71c3552c8eb482798e336690e0ac2b14b13015924810094f112045`.
The original `gateway-cpp-0.6.0` image is retained for rollback. The update is
installed and active. Only the `gatewaycpp` label was switched and reloaded;
the unrelated app services were not reloaded. A competing primary picker was
stopped by the user before activation. No robot command was sent by the update.

Post-restart verification preserved all 220 protected files, gateway
configuration, camera calibration/intrinsics, device speeds, stopped run and
durable completed-cup receipt. The in-memory gateway queue is idle after the
restart; its original DONE receipt remains durably recorded and cannot replay.
All three workers were fresh and armed, and the UI was ready with no error or
recovery requirement. The old orphan sockets were gone (zero established
gateway sockets in the post-install snapshot). A separate live keep-alive
health request closed cleanly after **5.005 seconds**, confirming the idle
connection deadline on the actual gateway.

The first camera-only scan after the restart took 6.120 seconds. Its five
consecutive frames span 1.001 seconds, provider execution took 0.732 seconds,
and the total application detection stage took 2.571 seconds. Offline replays
of its exact data could not reproduce the extra processing delay: detector,
validation and durable audit took 0.254–0.312 seconds, grid assessment took
0.315–0.354 seconds, and image/JSON persistence took 0.017–0.023 seconds.
No speculative geometry or durability changes were made. A subsequent warm
UI scan completed in **2.393 seconds** (2.311 seconds between UI event stamps),
with six detected cup candidates, unchanged queue/run and no robot commands.

These measurements establish the faster coordinator save, reduced redundant
image transfers and actual gateway idle-connection cleanup. They do not claim
an 84% reduction in scan duration or a measured end-to-end physical takeover
time. No cup batch or cable-disconnection test was started; the accepted-cup
completion barrier and the existing lease/contention timings remain unchanged.

## Validation evidence

The affected coordinator/queue/device-speed/continuous suites passed 487 tests.
The camera/inference/scan suites passed 225 tests plus 67 subtests. These counts
are overlapping checks and must not be added as a unique total. Strict JSON,
write and rename failures, file/directory synchronization ordering, old-file
compatibility and retained completion evidence are covered. A final focused camera, compact-write and gateway-restart check passed
47 tests plus 34 subtests. Source diff whitespace checks passed.

Evidence is kept in workspace `tmp/picker-performance-20261123/`, including the
Pi timing and socket reports, compact-writer benchmark, deployment results,
`final-live-verification.json`, `final-post-gateway-verification.json`,
`live-mac-scan.json`, `live-mac-warm-scan.json`,
`live-mac-warm-scan-adjacent.json`, `live-idle-timeout.json`,
`gateway-network-deployment-result.json`, network tests, source manifest
and gateway configuration baseline. Credentials are excluded from
these reports.
