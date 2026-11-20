# Lynx saved-scan rollback — 2026-11-19

The existing Lynx picker package at `192.168.125.20` was rolled back from
**0.2.11 to 0.2.10** through its existing `lynxcli` application configuration.
The restored package uses the complete normal `smooth_dual_arm_v1` handover and
saved-scan selection, without automatic Home or between-cup camera movements,
captures or source-cup rematching.

The picker service is online on port 8091 in **standby**, waiting for device
selection. It was inactive before the rollback and remains inactive:
`controller_present=false`, `ready=false`, `busy=false`, and no active ownership
generation. Its highest ownership generation was 57 before the update.
No live capture, robot movement, gripper command or controller activation was
performed. The Mac dashboard still has MacBook selected and lists Lynx as
available in standby.

## Restored behavior

- A cup uses 19 jobs, including 11 buffered paths. The smooth approach,
  handover, separation and turnover movements remain.
- Dual-arm travel TCP height remains 175 mm. After placement, the left arm
  stays above the handover and the right stays 80 mm above its destination.
- Later picks reuse the selected scan. There are no added wide-arm parking
  paths, receiving turns from those parking poses or captures between cups.
- Explicit live scans retain the previously requested grid reconciliation.
  Stale reservation markers are removed according to scan evidence. Grid
  bookkeeping changes do not interrupt an active handover.
- Existing confirmations, Stop and recovery records remain in effect.

## Package and state verification

All 35 checked application, model and startup files, plus two support files,
matched the original 0.2.10 manifest after reload: **37 files in total**. The
native worker also matched the original 0.2.10 binary.

| Item | Value |
| --- | --- |
| Source-manifest SHA-256 | `2f8fe1ddac8516c7ce70ab59a04b8747abc3d0c142e0cf75eda704d3f7c27897` |
| Native-worker SHA-256 | `88c661ac80d3a32b6ad2c791d29bd59145bac781cedd771bfe63d47c5c3bad27` |
| Verified plan fingerprint | `169d10f8cb23c038a3a00501f52875bd6d5789deee7a32c3081c94b9fc0e3148` |
| Restarted service boot ID | `41dc2097a28649ddb223857112addcd8` |

The removed feature module `workspace_check.py` is absent from the restored
package. Package identity and the original smooth-plan verification are recorded
in [the smooth handover deployment report](SMOOTH_HANDOVER_UPDATE_2026-11-19.md).

Before reload, 36 stable persistent files were backed up, together with service
logs and the PID record, under:

```text
/state/cli-0.2.0/backups/camera-rollback-0.2.10-20261119/
```

All 36 stable files were byte-for-byte unchanged after both package reload and
service restart. No saved state was reinitialized or replaced to perform the
rollback.

The offline verifier passed **on Lynx using the restored installed package**.
Both a two-cup batch and two individual picks produced 38 jobs from one capture;
each cup used 19 jobs and 11 paths, with `home_after_cup=false`. The verifier
issued zero network requests and zero hardware commands. Its plan fingerprint
matches the original smooth-release verification above.

The unchanged `start-device-service.sh` restarted the picker on port 8091 as
PID 7. The separate C++ gateway remained version 0.6.0; its uptime increased from
181,677.2 to 181,824.2 seconds across the rollback and service checks, confirming
it was not restarted. No gateway or RAPID changes were made.

WeOS confirms that `lynxcli` selects the valid 0.2.10 image. The running
configuration was saved with `copy run start`.

Select Lynx in the dashboard to activate its controller when ready to use it.
These checks verify the installed files and offline behavior. No physical pick
or device reboot was used to validate this rollback.
