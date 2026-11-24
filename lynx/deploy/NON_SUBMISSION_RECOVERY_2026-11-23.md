# Historical non-submission recovery

A queue that was never dispatched could block every later browser batch with
“Coordinator has not confirmed this non-submission; reconcile it first.” The
client retried an already confirmed rejection after a newer batch was admitted.
The coordinator required the old run to remain current, so that retry failed and
the client overwrote its durable confirmation flag with `false`.

The queue client now retains confirmed non-submissions. If an older client has
already lost the flag, reconciliation reads the exact archived run/submission
receipt to restore it. A record containing execution evidence cannot be treated
as unsubmitted; uncertain or contradictory results remain blocked.

The coordinator also returns the same confirmed rejection for the same worker,
run and submission after a newer run exists. This is an idempotent lookup, with
no gateway request or state write. Creating a rejection or changing any receipt
still requires the current run, and observed robot execution remains protected.
This compatibility fix lets already-running clients recover without restarting
the MacBook UI or losing its operator-confirmed inspection of an old arm error.

Regression coverage includes starting a new finite batch with confirmed and
previously damaged receipts, both with worker restarts and with the older client
behavior still running. These tests verify one new cup and measured Home
completion, with no replay of historical robot commands. Additional tests cover
historical receipt identity, outages, uncertain results and contradictory local
execution evidence.

Validation: 559 tests and four subtests pass, including 47 focused regression
cases for the client, coordinator and next-batch workflow.

Installed the coordinator compatibility fix on Lynx on 2026-11-23. Only
`failover_coordinator.py` and its source-manifest entry were replaced. The
combined coordinator/tertiary service restarted gracefully while the MacBook UI
and Pi worker remained running. All three roles subsequently reported armed.
The exact blocked run, historical receipt and idle gateway queue were preserved,
along with 63 protected source/state fingerprints. No robot movement, Stop,
queue clearing, recovery acknowledgment or new batch was sent.

A live compatibility check exercised the older client's exact rejection and
acknowledgment path using a temporary copy of the actual affected Mac receipt.
The updated coordinator returned its existing historical confirmation, and
acknowledgment succeeded. The live local receipt remained untouched; the running
browser can reconcile it normally on the next batch. The MacBook UI process and
its operator-confirmed inspection remained intact. Additional queue-client
hardening is in the local project source; this live installation changed only
the shared coordinator, which also supports the existing Pi and Lynx clients.

Local verification evidence is
`tmp/non-submission-fix-20261123/live-compatibility-verification.json` under the
workspace. Remote backup and deployment evidence are in
`/state/cli-0.2.0/non-submission-coordinator-update-20261123` on Lynx.
