# Failed batch recovery from the UI

The MacBook UI exposes **Unblock failed batch** when recovery is required.
After inspecting the robot, the operator confirms that both arms are stationary
and both grippers are empty, then clicks the button. Controller faults must be
resolved on the FlexPendant first. Recovery requires a new scan and a newly
confirmed batch; it never resumes the interrupted cup.

The existing `/api/recovery` route now retires a matching shared failed or
cancelled batch as well as its gateway queue. The Mac must acquire the primary
control lease. Active batches, running queues, another control owner, unknown
receipts and mismatched queue evidence remain blocked.

Before clearing a terminal gateway queue, the client archives its original
evidence. The coordinator independently checks the lease, idle gateway queue
and terminal arm statuses, then attaches an inspection record containing the
operator role, timestamp and SHA-256 of the exact original queue. The digest
binds the acknowledgement to its queue evidence. The original failed receipt
and failure reason remain unchanged; the stopped run detaches its current head.
The coordinator advertises `inspected_queue_recovery`, so an older coordinator
is rejected before clearing its queue.

Pi and Lynx can reconcile only a matching saved failed receipt against that
shared inspection. They preserve the failure, archive their previous local
record and invalidate the scan. They do not replay placement, Home or success
callbacks. An unrelated local recovery or a newer Stop remains latched.

Recovery sends no motion, Home, capture, robot Stop or RAPID reset. A lost
response does not trigger an automatic retry. Each UI attempt consumes its
inspection checkbox; a further attempt needs a fresh explicit acknowledgement.
If a queue clear succeeded but its response was lost, the next attempt observes
the idle queue and does not repeat the deletion. Coordinator and local-storage
failures can likewise be completed by a new explicit attempt without restarting
the old batch.

## Runtime files and checks

The device update installs only
`capture_and_detect/{failover_coordinator,failover_workflow,queued_robot_client}.py`
with source-manifest updates and backups. It leaves the gateway executable,
RAPID, movement plan, model, calibration and placement grid unchanged. This
supersedes the queue-client source hash recorded in the earlier handover update.

Deployment evidence and source payloads are under the workspace's
`tmp/recovery-button-update-20261123`. On Pi the backup directory is
`/home/kasun/picker-devices-20261113/recovery-button-update-20261123/backup`;
on Lynx it is `/state/cli-0.2.0/recovery-button-update-20261123/backup`.

The focused Python suite covers queue identity, recovery acknowledgement,
interrupted responses, local storage failures, returning workers, concurrent
Stop, coordinator startup and native runtime compatibility. Frontend tests cover
the button, inspection gating, duplicate clicks and refresh behavior. Tests use
mock gateways; no physical recovery or picking trial is implied.

Installation preserves the existing failed `left_park` queue and shared batch.
The operator must perform the inspection before using the new button.

The November 23 installation completed on both devices, with fresh armed
heartbeats and matching source hashes. All protected state hashes and the
gateway queue bytes remained unchanged. The MacBook UI was reloaded while
idle; its saved receipt, recovery journal and settings were preserved. The
browser showed the inspection checkbox unchecked and the recovery button
disabled until acknowledgement. No live recovery was requested.

Final verification: 444 Python tests and 49 subtests passed, plus 166 frontend
tests. Both devices passed the offline runtime verifier and accepted their
existing saved receipts under the updated readers.
