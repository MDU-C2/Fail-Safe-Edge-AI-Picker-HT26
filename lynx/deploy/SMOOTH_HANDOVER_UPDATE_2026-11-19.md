# Lynx smooth handover update — 2026-11-19

Package **0.2.10** is installed on Lynx at `192.168.125.20`. The picker service
has restarted with its existing launcher and is waiting for UI device selection.
It was inactive before this update and remains inactive: `controller_present`
and `ready` are false, with no active ownership generation. No live capture,
robot movement, gripper command or controller activation was performed.

## What was missing

The installed 0.2.9 source manifest matched the earlier deployment, before the
complete normal smooth handover adaptation in commit `7ea69e5`. Its
`handover_motion=blended_path` metadata described a partial implementation.
Pulling updated source on the development computer had not replaced that
installed Lynx image.

The new package includes the shared `smooth_dual_arm_v1` planner and its matching
robot client, session controller and native runtime. The native CLI and the
service used by the UI now load this same implementation:

- All dual-arm movements use buffered paths: 19 jobs, including 11 paths per cup.
- Cup approach, lift, receiving, separation and placement use the planned linear
  segments. Intermediate travel blends are 10 mm; the receiving corner is 3 mm.
- Paths use 60% acceleration and 40% ramp; carrying has a 1.5 m/s² limit and
  turnover uses 30% acceleration. Existing saved phase speeds still apply.
- Right-arm preparation overlaps left approach/grip. The handover approaches
  overlap, and both arms separate together after the grip/release barriers.
  Turnover waits for both separation movements.
- Dual-arm travel TCP height is 175 mm. Both grippers start and finish open.
- Each cup finishes placement before the next begins. There is no automatic
  Home, including after the last cup. The smooth branch's simulation-only
  inter-cup overlap is not part of this normal plan.

The package also includes the current, previously requested scan-driven grid
updates. A new scan reconciles stale reservations with its observation; grid
changes do not add a robot stop or per-job reservation validation. Picks still
reuse the selected scan without captures or cup rematching between cups.

## Package identity

Built from `vision_detection3` at `6fa7f6f019684f2025d6215f59b8f44c76774e23`
with the local scan-grid changes. The source manifest, rather than that commit
alone, identifies the deployed files. Credentials and saved robot state are not
part of the package.

| Item | Value |
| --- | --- |
| Version / target | 0.2.10 / Linux ARM64, CLI profile |
| Package size | 47,457,746 bytes |
| Package SHA-256 | `3ba010a7877090037bae83d2abe96b4a021a44718373e62636a6bc1322cebf6e` |
| Docker image ID | `sha256:ff419cce68645edadb6a8d7e6049f6fa87d1ba0fec058e419beaaa4b917762f9` |
| Source-manifest SHA-256 | `2f8fe1ddac8516c7ce70ab59a04b8747abc3d0c142e0cf75eda704d3f7c27897` |
| Handover planner SHA-256 | `fc31fca4ed3b18959cea60462848bb5c0e9bbcfd292e13774946fd7648bff682` |
| Robot client SHA-256 | `b79a0e5ea950901d2eec3a18844eb93a0c1cce2a354fbfc81cbcf7dd2f788ffd` |
| Cup session SHA-256 | `196c2152d19b42b3a75f66b7d3750d16a89e7f3a4051c32d61f8a851ab39bbec` |
| Native worker SHA-256, unchanged | `88c661ac80d3a32b6ad2c791d29bd59145bac781cedd771bfe63d47c5c3bad27` |
| Model binary SHA-256, unchanged | `3d1167d533a8ad907caf9673ed1374e9f2977093dc89e1ad65758a79bec93bb8` |
| Signature | Unsigned; explicitly accepted during WeOS import |
| Persistent state | `/state/cli-0.2.0` |
| Service boot ID | `66538b9adf3c476d9df84ab945adcad2` |

## Verification and preserved state

The ARM64 image passed the standalone offline verifier with Docker networking
disabled. The installed Lynx files passed the same verifier using the packaged
Python environment, with socket and HTTP calls blocked. Both runs checked the
exact path geometry, acceleration settings, dependency barriers, low-height
linear endpoints and no-Home behavior. Two-cup batch and individual selection
each produced 38 jobs from one scan, with no robot or network commands.

Both runs produced plan fingerprint
`169d10f8cb23c038a3a00501f52875bd6d5789deee7a32c3081c94b9fc0e3148`.
All 37 installed application, model, startup and support files matched their
source-manifest hashes. The native worker binary also matched the tested image.

All 39 backed-up persistent files were byte-for-byte unchanged before and after
the service restart. These include calibration, grid/table reference, motion
settings, ownership, credentials and recovery records. The backup and deployment
evidence remain on Lynx at:

```text
/state/cli-0.2.0/backups/smooth-0.2.10-20261119/
  sha256.json
  source-manifest.json
  lynx-picker-0.2.10.wmopkg.json
  installed-verification.json
  verify_smooth_runtime.py
  offline-verification.json
  pre-deployment-health.json
  post-deployment-health.json
```

Only `cuppicker` was reloaded through its existing `lynxcli` image label. Its
existing `start-device-service.sh` restarted the service on port 8091. The
separate C++ gateway remained version 0.6.0, with uptime increasing from
165,560.6 to 165,677.9 seconds across the reload. Gateway health reported no
active control lease, no movement and an idle command queue. No gateway code,
gateway process or robot RAPID program was changed. The new picker continues
using the existing path/gripper API; this update does not enable queued motion.

Select Lynx in the UI to activate its controller and obtain a new scan. These
checks verify installation and offline behavior; physical smoothness and
persistence across a Lynx reboot have not been tested for this release.
