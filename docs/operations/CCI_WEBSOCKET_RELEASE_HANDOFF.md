# PR #150 Runtime release handoff — operator approval required

This DEV phase authorizes GitHub merge after its gates. It does not authorize
Runtime deployment, process operations, production database access or .env edits.
The exact resulting merge SHA is recorded in `CCI_P0_WEBSOCKET_FINAL_RELEASE_REPORT.md`
and PR #150's merge record after release; use that immutable release identifier.

## Compatibility and preparation

- Current observed Runtime checkout: `235ec88c09ff6df0372bd636eaaf47814e48bd6d` (#149).
  Reverify at rollout; the observation is historical, not a current process audit.
- Dependencies/schema/epoch requirements are unchanged. The existing project Python
  and websockets `>=15.0,<17.0` are used; observed Runtime was Python 3.12.10 /
  websockets 16.0, while DEV and CI validate Python 3.11. No new package is required.
- Verify a clean source checkout, its exact revision, existing environment and the
  established scanner launcher/arguments. Preserve strategy flags, runtime identity,
  epoch, database path, watch interval and Telegram delivery identity.
- No migration, checkpoint, database copy, cleanup, data repair or .env edit is
  required by this release. Keep the 11 unresolved canonical references protected;
  source rollback or transport recovery does not authorize alert replay.

## Controlled rollout and consumers

1. Obtain separate owner approval for the Runtime maintenance window and restart.
2. Use the existing owner-controlled stop procedure for the scanner watch process.
   Its virtual-environment redirector and Python worker are one process chain;
   do not launch a duplicate or broadly kill all Python processes.
3. Before updating files in the shared checkout, also stop any Telegram listener
   or command consumer importing that checkout. The prior inventory showed no
   matching listener; verify current state instead of assuming it is stopped.
4. Apply the exact approved merged SHA using the established clean, fast-forward
   release procedure. Stop on local modifications, unexpected ancestry or an
   unexpected release identity; preserve unrelated local artifacts.
5. Under the existing Runtime environment, check dependency consistency, compile
   the changed modules, verify the release revision and the existing identity/epoch
   startup safeguards. This release changes no schema or strategy thresholds.
6. Restart the scanner once with the established verified launcher. It must restart
   to load the changed feed services. Resume only previously intended listener /
   command consumers after the update; the release does not call for a new listener.

Exact process stop/start commands depend on the operator's verified launch method
and arguments; the DEV phase does not reconstruct them from README defaults or
from credentials. Do not alter cadence, .env or Telegram identity to make recovery
look successful.

## Acceptance evidence

Observe at least five consecutive complete scanner cycles. Record actual configured
interval, scheduled/actual start, final duration, missed intervals and all phase
statuses. Do not interpret low evaluated counts without rejected/error/not-run
accounting. Telegram PARTIAL may remain due to the separately confirmed provenance
issue; it is not a WebSocket health signal.

For each scanner-owned feed retain the route, upgrade result, matching subscription
ACK where applicable, actual accepted USD-M USDT-perpetual event counts, source /
receipt timestamps, rejection/error categories, close codes, failure streak, retry
delay, suppressed-log count and stable recovery message. A handshake or an ACK is
not market-data verification. A quiet liquidation stream needs confirmed subscription
coverage; actual liquidation parsing remains unverified until genuine events arrive.

Require fresh book snapshots, snapshot-overlap bridge, continuous pu/u IDs, immediate
loss of authority on disconnect/corruption, fresh synchronization after reconnect,
and explicit incomplete-band flags. Do not treat a fresh synchronized partial-depth
book as complete liquidity-band coverage. Preserve microstructure coverage gaps.

Within a separately approved fault test, disconnect only the designated owned feed
once and verify bounded attempts, invalidation, fresh recovery and no duplicate
socket/process. Never conduct an aggressive reconnect loop. Check CPU/RAM and
scanner responsiveness across the cycles and again through natural 24-hour rotation.

Known event-loop stalls remain: see `CCI_WEBSOCKET_EVENT_LOOP_FOLLOWUP.md`. They can
still delay these feed tasks. Code merge and a short standalone probe do not prove
sustained production recovery.

## Rollback

If acceptance fails, the operator obtains rollback approval, stops the same consumers,
and restores the prior verified source release (observed baseline `235ec88`, reverify
before rollout) using the established safe procedure. Restart with the same verified
launch configuration. Preserve all database/WAL files, .env, epoch and public-event
history. This source-only release requires no database rollback or event resend.
