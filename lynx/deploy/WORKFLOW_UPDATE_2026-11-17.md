# Lynx workflow update — 2026-11-17

Package **0.2.9** is installed on Lynx and its service reports ready. Installed
hashes match the build, and all 11 recorded persistent-state files matched their
pre-update bytes before the service restarted. No live camera scan or robot
movement was requested for this update. Physical picking and reboot persistence
have not been tested for this release.

## Workflow

The native CLI and the Lynx service used by the Mac dashboard share the updated
controller. A scan makes one perception observation with up to five available
depth samples. Individual picks and batches reuse that scan: there are no
captures between cups, selected-cup rematching or observation-age expiry.
Completed cups become unavailable while other cups remain selectable.

The shared planner uses smooth handover paths and the fixed receiving-hand
positions. Completed picks finish above the destination; Home remains a separate
operator command. Placement evidence, reservations, confirmation, Stop and
recovery handling remain part of the enhanced workflow.

The rebuilt C++ worker applies the V4 object-depth rule to available positive
depth samples. The removed core/spread rejection no longer blocks cup selection.
Saved depth and placement evidence use the latest aligned raw frame. The frozen
V4 terminal is separate and is not included in this lightweight package.

## Package identity and validation

| Item | Value |
| --- | --- |
| Version / target | 0.2.9 / Linux ARM64 |
| Package size | 47,457,718 bytes |
| Package SHA-256 | `edbd5b68518126e4051dbb91459433a110150622746a25defac6c4c45415fab8` |
| Source image | `sha256:ad52655728b5506d12b59c808ec7761705da83d3c8c3b43dec1b969e09e7d04e` |
| Source-manifest SHA-256 | `4f20234443b4d38328b39a6babcef4a40e2993c970fc00e5c3fa3b050c59ad78` |
| Installed native worker SHA-256 | `88c661ac80d3a32b6ad2c791d29bd59145bac781cedd771bfe63d47c5c3bad27` |
| Signature | Unsigned |
| Entrypoint / default mode | `/start.sh` / dry run |
| Persistent deployment state | `/state/cli-0.2.0` |

The package stages 60 allowlisted source/model/build files. Source hashes were
verified; the existing INT8 model is unchanged. Credentials, calibration,
placement grids, recovery records and captures are excluded from the package.

Validation completed before deployment:

- 139 offline tests and 243 subtests passed, including snapshot reuse,
  no automatic Home, handover sequencing, native runtime and package staging.
- The ARM64 native core test passed during the build.
- Lightweight import checks passed without Python image, ML, camera or web
  dependencies.
- Three offline scans passed in the built ARM64 image with networking disabled.
  Each used one observation, one inference pass and five synthetic repeated
  depth samples. Scratch grid/reference hashes stayed unchanged. This saved
  scene has no selectable cups and occupied destinations, so no transfer was
  attempted; it is not fresh camera evidence or a motion test.

The package manifest and local `validation-0.2.9.json` record the hashes above.
The earlier [0.2.5 camera report](CLI_CHECK_2026-11-13.md) is historical evidence,
not a live validation of this release.

## Deployment verification

WeOS validated the imported 0.2.9 package and its SHA-256 matched the artifact
above. Only `cuppicker` was reloaded through its existing `lynxcli` image label;
the separate gateway stayed running at version 0.5.0. All 35 installed
application/model/startup file hashes matched the source manifest, and the
installed native worker matched the built image.

Before service restart, all 11 recorded persistent-state files were byte-for-byte
unchanged, including calibration, placement state, table reference and recovery
records. The existing INT8 model and P2/P3/P4 reservations were preserved. A state
backup is retained at `/state/cli-0.2.0/backups/workflow-0.2.9-20261117`.

The service restarted through its existing startup script and the Mac dashboard
reactivated Lynx. The new boot ID is `d8c45a3bc81e440eb61882ef554f58e4`. Its status
reported ready, idle, no error, no recovery requirement and no usable scan.
Runtime metadata confirmed `workflow=v4_snapshot`, `verification_frames=0`,
`per_cup_capture=false`, `home_after_cup=false` and
`handover_motion=blended_path`. Saved speed settings were retained, including
1000 mm/s travel and 100 mm/s pick/flip. The 128 MiB memory startup guard passed
with 139,014,144 bytes of measured headroom.

After dashboard activation, 10 of the 11 saved files remained byte-for-byte
unchanged. Only `device-owner.json` changed as expected for the new service boot
and activation generation. Placement-grid contents and saved phase speeds were
equal before and after activation, and the recovery journal was unchanged.
Read-only gateway health reported healthy, no control lease and no movement in
progress. The dashboard reported Lynx active and ready, with no error or usable
scan. These results are recorded in the local `deployment-0.2.9.json` artifact.
The WeOS running configuration was saved with `copy run start` after verification.

These checks establish the installed code and runtime policy. They do not
validate a live scan, physical handover, or persistence across a device reboot.
