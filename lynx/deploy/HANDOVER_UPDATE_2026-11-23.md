# Shared handover timing update — 2026-11-23

The movement update pulled into `vision_detection3` at `8d9fc16` (motion
commit `b110005`) is used by the MacBook browser workflow and the Pi/Lynx
failover workers through `handover_plan.handover_jobs`.

After the receiving arm grips the cup and the left gripper opens:

1. The left arm rises straight out of the cup at the pick/lift phase speed
   (100 mm/s by default, capped by the selected overall speed). The right arm
   lowers the cup at the same time.
2. The right arm turns the cup over only after both separation moves finish.
3. The left arm moves to its existing side parking pose only after turnover.
   It can park while the right arm places and releases the cup.
4. The existing right-arm camera parking move follows placement clearance.
   Capture waits for the whole cup queue, including both parking moves.

The tuned coordinates, rotations, camera/grid refresh, finite failover batches,
and Home policy are unchanged. No Home command is added between cups. This
update does not modify the gateway or RAPID program.

## Queue scheduling and saved evidence

`queued_robot_client.py` now ends an arm lane's group at a step whose completion
releases the other arm. Both continuations can then share the next group. This
preserves the shared plan's simultaneous approach and separation, and allows
left parking to overlap placement after turnover. Every same-arm order and
cross-arm dependency remains enforced.

New pending records explicitly store `grouping_version: 2`. Records without
that field retain version 1 validation against their exact original jobs and
queue groups. Existing successful and failed receipts are neither rewritten
nor reinterpreted as newly dispatched work. Unknown versions and modified job
bodies/groups are rejected. Legacy grouping is available only for reading
saved evidence; new submissions always use the updated grouping.

## Runtime installation

The MacBook UI uses the repository source and was restarted while idle. Pi
uses `/home/kasun/picker-devices-20261113/capture_and_detect`; Lynx uses
`/state/cli-0.2.0/armed-runtime-20261120/app/capture_and_detect`.

The device payload contains the shared `handover_plan.py` and the scheduling
and receipt-version changes applied to each device's existing queue client.
Unrelated Mac-only recovery additions are not introduced into those installed
workers. The deployment payload, exact before/after hashes and minimal diff
are under the workspace's `tmp/movement-update-20261123` directory. Models,
calibration, placement grid, credentials and saved robot evidence are retained.

Both device installations completed, and the Mac, Pi and Lynx workers reported
fresh `armed` heartbeats. Lynx's combined service was restarted; Pi's existing
armed user service was started. Backups and deployment evidence are retained
under `/home/kasun/picker-devices-20261113/movement-update-20261123` on Pi and
`/state/cli-0.2.0/movement-update-20261123` on Lynx. Pi's older source
manifest was preserved and only the two changed source entries were updated.

Installed device source hashes:

| File | SHA-256 |
| --- | --- |
| `handover_plan.py` | `d6e03289cbed3f9f3f48910f8eccee3454ec43eac1fa5c0d7f297414a3c383a1` |
| `queued_robot_client.py` (minimal device patch) | `22c535d0eb30464621a45f5db24817cc6e4c16cdc49bca2bd896d63cd19d35e3` |

## Verification

- 191 motion, direct-executor and queue tests, plus 341 subtests, passed.
- 329 failover/coordinator/UI/queue/native-runtime tests passed (the queue tests
  overlap the first set).
- The offline runtime verifier checks the new separation/turnover/parking
  barriers as well as moving/new cups, freed grid cells and finite completion.
  It blocks real network access and starts no camera or inference worker.
- The Mac restart preserved the existing failed pending record and gateway
  queue byte-for-byte. The blocked November 23 `left_park` run was not cleared.
- Both devices passed the offline verifier before installation, and validated
  their existing pending receipt. Eleven protected Pi state files and fourteen
  Lynx state files remained unchanged. Installed plans and serialized queues
  matched the Mac for the same cup and placement inputs. The offline verifier's
  reference plan SHA-256 is
  `78819c0c7cee516ec06c5d5f6b20a2a6977f2cc9193f20d583b4a96537dfd15b`.

No physical picking, unplug failover or collision-clearance trial is performed
by these software checks. A saved failed run remains blocked for recovery.
