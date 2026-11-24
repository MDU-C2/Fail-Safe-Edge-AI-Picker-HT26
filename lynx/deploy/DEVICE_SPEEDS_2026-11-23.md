# Separate device speeds for browser batches

Settings → Movement speeds has separate overall-speed fields for MacBook,
Raspberry Pi and Lynx in the automatic failover workflow. Apply all three before
preparing a batch. Existing single-device workflows retain their overall-speed
field. Stage-speed limits remain shared; each stage uses the lower of its limit
and that device's overall speed. Existing Home limits are preserved.

Applying the fields saves `device-speeds.json` in the primary UI's state directory
and cancels pending previews. Initial values follow the current overall speed;
installation does not choose new movement speeds. The file survives UI restarts.
Inputs must be finite numbers between 1 and 1500 mm/s. Changing speeds sends no
robot command or camera request and cannot edit an active, finalizing or watching
batch, including a batch currently owned by another device.

The confirmed shared run carries an immutable `device_speeds` map keyed by
`primary`, `secondary` and `tertiary`. A worker selects its role's speed before
generating new cup plans. Already accepted queues retain their original steps
and speeds. A returning primary restores its saved preferences after executing
an inherited run. Older runs without a profile retain their original shared
speed. The coordinator advertises `device_speed_profiles`; applying or starting
a profiled batch requires that capability.

The dashboard shows the three applied values independently from edited values.
Unapplied edits survive status polling. Stop, recovery, disconnected status and
active work keep Apply disabled. The server also checks shared run state before
saving, so an idle browser cannot change an active remote batch.

Implementation touches the shared failover workflow/coordinator, the MacBook
web controller and dashboard assets. No gateway, RAPID, motion geometry, camera,
grid or inference changes are needed. Pi and Lynx need the two updated shared
Python modules before the UI is reloaded with this feature.

Installed and verified on MacBook, Raspberry Pi and Lynx on 2026-11-23. Following
network reconnection, the authorized Stop was confirmed by RAPID; both motion
tasks reported stopped and gateway control was inactive. Each reload required
the exact completed 19-step queue and an unchanged terminal shared run. The
blocked run and recovery records were preserved. No recovery was acknowledged,
no new movement was started, and no new speed values were selected.

Both remote workers passed the packaged offline checks before installation.
The final coordinator check reports all three roles armed with no active run,
and advertises `device_speed_profiles`. The MacBook UI is ready and shows the
three fields under Settings → Movement speeds. Their initial values remain
1000 mm/s, matching the prior shared overall setting. Apply remains disabled
until the existing inspection/recovery requirement has been resolved.

The existing **Stop robot** button at the top of every UI tab sends this same
software-stop request and retires the shared batch when the services are
reachable. A stop blocks further local commands immediately. Inspect the robot
and use the displayed recovery process before resuming; the physical emergency
stop remains the control for immediate stopping.

Verification: 58 profile tests exercise generated cup and Home queues for every
role and primary → Pi → Lynx → primary transitions; 204 existing failover tests
pass. The broader UI/controller regression run passes 175 tests and 235 subtests,
including 14 new HTTP/controller profile tests. All 169 dashboard tests pass.
The packaged offline runtime verifier confirms role speeds, persistence, legacy
behavior and active-batch rejection with zero real network or model calls.
Deployment scripts, exact candidate hashes, remote verifier results and final
readiness evidence are in the workspace's `tmp/device-speed-update-20261123`.
`final-all-device-readiness.json` records all three armed workers, the stopped
robot, unchanged completed queue and preserved recovery requirement. MacBook
saved-state hashes remained unchanged; the remote checks preserved 58 Lynx and
76 Pi source/state fingerprints and the generated motion/queue fingerprints.
Backups of the two replaced modules and their manifest are on each device:

- Lynx: `/state/cli-0.2.0/device-speed-update-20261123/backup`
- Pi: `/home/kasun/picker-devices-20261113/device-speed-update-20261123/backup`

Follow-up: a successful cup can retain `reconciled: false` after every movement
and local completion callback has finished. The primary speed setter now accepts
that historical receipt after validating its complete gateway snapshot, owner,
step signature and start time. It leaves the receipt, placement records and
recovery journal untouched. Incomplete callbacks, missing Home evidence and
failed or uncertain receipts still block changes. The 80 workflow profile tests
and 14 HTTP/controller profile tests pass, including 22 added regression cases.

This follow-up fix was loaded into the MacBook UI after the user authorized Stop
and both motion tasks confirmed stopped. A newer, uncoordinated 18-step queue
had failed at `right_via` after the left gripper closed while the UI remained
idle. The guarded reload preserved this exact failure, the blocked shared run,
saved settings and recovery requirement. The UI is ready, with all three speed
values still 1000 mm/s. No queue recovery, new movement or speed selection was
performed; Pi and Lynx need no restart for this primary-only settings change.
`mac-completed-receipt-fix-result.json` in the deployment evidence directory
records the loaded source hash and successful state-preservation checks.
