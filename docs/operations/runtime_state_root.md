# Isolated runtime state root

`scripts/run_scan.py` and `scripts/run_telegram_bot.py` accept
`--runtime-state-root PATH`. The equivalent setting is `RUNTIME_STATE_ROOT`.
The explicit CLI argument takes precedence over the setting. Without either,
existing path behavior is preserved.

This is a storage-routing option. It does not initialize a database or epoch,
migrate historical state, change strategy rules, enable Telegram delivery, or
authorize production startup. A deployment still needs its reviewed release,
supported epoch identity, ownership checks, and operator approval.

For an approved fresh generation, select an unused dedicated directory, such as
`S:\CCI_RUNTIME_V2`. Preserve the legacy `scan_runs` junction and its target.
Use the same root for both entrypoints.

| State | Location under the configured root |
| --- | --- |
| Default SQLite DB, including outbox/delivery state | `candle_craft.db` |
| Epoch-specific watch state | `epochs/<active-epoch-id>/watch_state.json` |
| Latest saved scan / latest-run lookup | `latest_scan.json` |
| Scanner manifest | `scan_run_manifest.jsonl` |
| Nightly history | `nightly_scan_history.json` |
| Performance memory | `performance_memory.json` |
| Admin scan drafts | `admin_drafts/` |
| Listener polling state | `admin_commands/state.json` |
| Listener command audit | `admin_commands/commands.jsonl` |

Watch lease, session receipt, and bounded observation journal remain adjacent
to the selected database. Runtime epoch identity and Telegram outbox authority
remain in that database. The complete expected epoch tuple is still required;
the state root cannot manufacture or replace it.

Relative explicit database, cache, resume, output, export, and listener state
arguments resolve beneath the configured root. Absolute overrides must also
resolve beneath it. Existing shorthand such as `--save-run` uses its filename
directly below the root rather than below the legacy `scan_runs` directory.
Optional exports and file caches remain disabled unless requested.

Both entrypoints validate paths before opening a lease, constructing a listener
service, or opening their operational database. Roots overlapping legacy
`scan_runs` storage are rejected. Windows junction and symlink aliases that
escape the root are rejected, including fixed scanner destinations. Paths are
passed per invocation and captured before disk-worker dispatch; configuring one
invocation does not rebind another invocation's global paths.

This contract covers the scanner and Telegram listener entrypoints. Standalone
maintenance/research scripts must continue to receive their own explicit paths.
The CMC sidecar has its separate existing `CMC_SIDECAR_PATH` contract; do not
activate CMC as part of a CoinPaprika-only fresh-generation rollout.

Tests use temporary databases and synthetic epoch identities. They prove path
routing and legacy-file preservation, not real market acquisition or production
watch acceptance. Production still requires two naturally completed iterations
and separate approval before public signal delivery.
