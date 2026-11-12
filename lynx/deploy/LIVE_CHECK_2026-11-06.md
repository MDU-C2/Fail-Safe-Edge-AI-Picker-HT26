# Lynx deployment check — 2026-11-06

**Work is paused at the user's request. Lynx management recovered after a power
cycle, and the Lynx picker is stopped with autostart disabled. The Pi gateway has
been restarted and its camera and controller connections passed a health check.
Version 0.1.0 passed synthetic scans, but the complete real-camera picking stack
has not been validated. No robot movement was performed.**

The check date uses the development Mac's UTC clock. The Lynx clock reported 1970,
so target wall-clock timestamps are not reliable evidence of the run date.

## Target and version 0.1.0 artifact

| Item | Observed value |
| --- | --- |
| Device | Westermo Lynx 3510-E-F2G-P8G-LV, WeOS 5.30 |
| Management address | `169.254.219.127` |
| Reported RAM | 496,652 KiB, approximately 485 MiB |
| Hosting allocation | 50% CPU and 50% RAM; these are configured limits, not measured utilization |
| Shared application memory limit | 254,283,776 bytes, approximately 242.50 MiB |
| Persistent SD state | Media `external`, path `app/lynx-picker/state`, mounted at `/state` |
| Package storage on SD | Media `external`, path `app/images` |
| Package | Unsigned ARM64 `lynx-picker-0.1.0.wmopkg`; package validation and checksum verification passed |
| Package SHA-256 | `e09c40c35b88b76c412f01fbe2d2e34304537feae640b2b1bafa16da74526bba` |
| Source image ID | `sha256:8f7da557832717681f949a75ea055a6bc1ee34bba88ac8f9c57a1ef340abdcda` |

The image identity and package hash are recorded in the generated
`lynx-picker-0.1.0.wmopkg.json` sidecar. Four previously hosted Lynx applications
were already stopped before this work; the memory allowance is shared across
hosted apps.

## Version 0.1.0 checks and results

| Check | Result |
| --- | --- |
| Real OAK startup | Failed with `X_LINK_DEVICE_NOT_FOUND`; no inference ran in this attempt |
| Camera discovery | `169.254.1.223`, MXID `1844301051DA9D0F00`, reported `BOOTED`; no available device was returned |
| Failed camera-start peak | 223,715,328 bytes, approximately 213.35 MiB |
| Synthetic scans on Lynx | Three passed, using 640×480 mock RGB/depth and the real 320×320 FP32 native model |
| Complete scan durations | 8.71 s, 6.71 s and 6.72 s |
| Native inference per frame | Approximately 1.16–1.20 s |
| Synthetic-run leaf cgroup peak | 220,278,784 bytes, approximately 210.07 MiB |
| Loaded synthetic idle snapshot | Leaf 207,409,152 bytes; shared parent 208,162,816 bytes |
| Python process RSS / high-water RSS | 93,420 / 98,844 KiB; excludes the native worker and other cgroup charges |
| Parent `oom_kill` counter | Historical value 6; unchanged during these checks |
| Access controls | Unauthenticated gateway access returned 401; dry-run mutations returned 403 |

Each synthetic scan exercised the coordinator's three-frame placement assessment.
The saved dashboard results reported `ready: true`, `busy: false` and
`mode: dry_run`. These checks demonstrate that the packaged gateway, coordinator
and native inference run on Lynx. Synthetic images do not establish cup-detection
quality, valid physical coordinates or grasp success.

The two memory peaks describe separate runs. The failed OAK attempt did not load
the native inference worker, and the synthetic run did not exercise the real OAK
pipeline. Neither peak establishes that the complete real-camera stack fits.
Leaf cgroup usage also must not be mistaken for total shared hosting usage. The
unchanged OOM counter indicates no new recorded OOM kill in this observation
window; it is not a long-run stability result.

## Version 0.1.1 deployment and interrupted real-camera check

The follow-up package reduces placement-calculation temporary buffers and releases
previous frame images earlier. It has passed a network-disabled, read-only Docker
synthetic scan and package read-back verification of 87 runtime/model/startup
files. Its worker is AArch64 and deployment credentials are absent from the image.
These are development checks and do not establish target memory requirements.

- Package: `lynx-picker-0.1.1.wmopkg`, 167,323,062 bytes, unsigned.
- SHA-256: `aa60d6b45547ea33b2d0eb832a4f3d902b2761cb31441eef68de765007083bb0`.
- Image ID: `sha256:5ff5bb3ed872d30d98c2bab14ae00555d763cde29397dd095aa82dec7ea7edf8`.

Version 0.1.1 was installed on Lynx and the deployed source hashes were checked.
No synthetic scan of this version was run on Lynx; the next target check used the
real camera.

With user-provided access to `rpi5.local`, the old gateway's health response showed
`control_lease_active: false` and `move_in_progress: false`. Its Docker container
`robot-gateway` was then stopped and remained `Exited`, with restart policy
`unless-stopped`. The application on Lynx was never started with `--live`.

| Real-camera startup observation | Result |
| --- | --- |
| Server startup | Reached ASGI startup complete; model-ready status had not been observed |
| Last observed shared parent cgroup usage | 231,407,616 bytes, approximately 220.69 MiB |
| Lynx management | Both SSH and HTTP became unresponsive |
| Network forwarding | ICMP replies remained around 0.7 ms, and SSH to the Pi through Lynx continued |
| Failure cause | Unconfirmed; post-failure cgroup/OOM counters and logs were not retrieved |
| Recovery | User subsequently power-cycled Lynx; management recovered using Mac source address `169.254.1.10` |

The last memory sample is not a peak or proof of an OOM event. The historical
`oom_kill: 6` observation above belongs to the earlier 0.1.0 checks and cannot
establish what happened during this later interruption. No successful real-camera
scan, model-ready state or complete-stack memory fit was demonstrated.

## Recovery and later bounded startup attempt

The Mac had two interfaces on link-local `169.254` networks, which confounded
reachability during the earlier interruption. Management was reachable after the
power cycle when the connection explicitly used source `169.254.1.10`. This does
not establish an OOM cause for the earlier loss of management.

After recovery, host `MemAvailable` was 364,028 KiB and the host's reported
shared-memory usage was 13,088 KiB. That usage fell by approximately 100 MiB after
reboot, consistent with clearing the failed import's temporary allocation; its
exact staging file was not identified. A later real-camera attempt used
640×360 at 1 FPS and reached the camera phase before the new model-start guard.
It reached cgroup usage of 254,144,512
bytes, with the `max` event counter at 827 and OOM counter at 0, then was stopped.
The incomplete failure log was lost with temporary storage; no particular
startup exception is attributed to this attempt.

An attempt to set hosting RAM to 60% was rejected: this WeOS version permits
10–50%. The configured RAM allocation remains 50%.

## Paused configuration and resume checkpoint

- Reboot restored the previous startup configuration with five app entries,
  including broken `app1` referencing missing `hello-lynx-1.0`. The original startup
  configuration was saved as `config1.cfg`; its `app1` metadata was also backed up
  locally at `tmp/lynx-live-check/app1-before-picker.txt`.
- Only that broken entry was replaced with `lynxpicker` in the running
  configuration. These changes were **not saved** to startup configuration.
- The current picker entry selects the verified 0.1.1 image, has persistent SD
  state/image mounts and autostart disabled, and is **stopped**. Its configured
  command is `/bin/sleep infinity`, not the picking application. Quoted command
  arguments were verified to work on this WeOS 5.30 device.
- Trial startup-guard overlays for `app.py` and `resources.py` were staged
  separately. Their stage-time hashes began `929d74e` and `8a8ca33`; later minor
  repository changes mean these overlays must not be treated as the final source
  version. The guard's 111 tests passed, but it has not been packaged.
- The SDK investigation identified roughly 32.7 MiB of retained buffer capacity.
  Tests covered 21 entries, and an offline diagnostic reported about 32.8 MiB RSS
  saved. This is not a measured real-camera saving on Lynx. The custom SDK wheel
  build was stopped and its builder container terminated; no new wheel or
  **0.1.2 package** is a completed deployment artifact. Source/build/downloads
  remain under `tmp/lynx-build-prep/depthai-src`; the preserved dependency cache
  is under `tmp/lynx-build-prep/depthai-memory/vcpkg-cache`. Dependency 28 of 98
  completed; work stopped during FFmpeg source extraction (29 of 98).
- The Pi gateway was restarted at the user's request. Its health response was
  `ok`, version 0.3.0, at 18.9 seconds uptime, with strict authentication. Camera
  `connected` and `ok` were true, frame age was 0.206 seconds, the robot controller
  was reachable, `move_in_progress` was false and no control lease was active.
  The final Lynx check confirmed `Stopped`, `Autostart: No` and the sleep command.
  No credentials are included in this report.

On an explicit request to resume, first inspect the running and saved app
configuration and Pi camera ownership. Review the final guard and SDK changes,
build a fresh artifact when ready, and verify its deployed hashes before another
bounded dry-run startup. Preserve logs on SD and measure shared cgroup usage and
events throughout startup, scans and reconnects. Physical coordinate and grasp
validation remains outstanding; neither synthetic results nor the offline SDK
diagnostic establishes that the complete real-camera stack fits.

Supporting local artifacts are under workspace `tmp/lynx-build-prep/` (package
sidecars and verification) and `tmp/lynx-live-check/lynx_check_*.json` (redacted
dashboard results). `tmp/lynx-live-check/resources-summary-0.1.0.txt` preserves the
target resource summary from `/state/mock-check/resources-summary.txt`.
