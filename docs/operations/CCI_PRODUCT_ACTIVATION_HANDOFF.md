# CCI product stabilization and activation handoff

## Reviewed scope

This change freezes Binance tick-size evidence from one shared acquired
exchange-info response before an implicit V2 scanner decision time. The
previous per-symbol lookup could refresh its 300-second cache during a scan;
that new acquisition followed the fixed cutoff and correctly failed the
causal precision gate, unnecessarily withholding earlier valid evidence.

The snapshot retains exact instrument identity, endpoint and acquisition time.
The existing 300-second freshness, no-future-data, precision tolerance,
0.618/0.750/0.786, closed 1D/12H/4H/15M, structural/liquidity stop, net TP2 RR
>= 3.0, quality and public eligibility rules remain intact. A failed snapshot
or malformed/missing instrument stays unavailable for that scan. It cannot be
replaced with post-cutoff precision. Explicit historical cutoffs and other
adapters retain their existing fail-closed acquisition behavior.

Tests exercise actual synthetic LONG and SHORT scanner/risk plans, two-cycle
lifecycle confirmation, persistence and fake transport with deduplication at
all three Fibonacci targets. Synthetic outcomes are not production evidence.

## Storage correction on the existing codec

Runtime PID 13452 explicitly inherited SCAN_RAW_PAYLOAD_INLINE_ONLY=true.
Bounded indexed inspection of three recent scans found inline parent payloads
of 9,516,342-9,538,193 bytes and child payload totals of 9,329,927-9,351,625
bytes per 55-symbol scan. The existing codec accepted exact child-byte equality
and produced a 191,744-byte reference parent for the inspected scan.

A separate disposable minimal-schema fixture measured 18,948,096 bytes of net
SQLite growth with inline storage and 9,588,736 bytes with references: a
49.39% reduction. Both hydrated to the identical logical payload. This is
fixture evidence, not a live before/after acceptance result. Lifecycle/outbox
history and old scan rows must not be rewritten or removed.

The proposed supported launch override is SCAN_RAW_PAYLOAD_INLINE_ONLY=false.
All readers must remain on decoder-compatible releases. Remaining growth is
still approximately one full symbol-result copy per scan; long-term capacity
is finite and must be reported honestly. No retention framework is added.

## Activation gates and prepared configuration

Deployment needs owner approval of the exact reviewed release SHA. Preserve
the existing V2 epoch tuple, generation binding, database and state root.
Use the supported Runtime launch profile and a receipt-verified graceful
scanner transition. Never run two watch loops or reuse previous-generation
listener state.

Scanner: retain --lifecycle and --store-scan; select
--telegram-manual-signals with --telegram-live-alerts false. The source routes
this to the canonical lifecycle-backed service and suppresses legacy watch
activation publishing. Set TELEGRAM_SIGNALS_ENABLED=true,
TELEGRAM_DRY_RUN=false and TELEGRAM_PUBLIC_SIGNAL_POLICY=lifecycle only after
the storage, eligibility, ownership and delivery gates pass.

Listener: TELEGRAM_COMMANDS_ENABLED=true and TELEGRAM_PUBLIC_UI_ENABLED=true;
use --runtime-state-root S:\CCI_RUNTIME_V2, database candle_craft.db,
manifest scan_run_manifest.jsonl, state admin_commands/state.json and audit
admin_commands/commands.jsonl. Verify zero other polling owners before startup.
Keep research, briefing and unrelated watchlist publication disabled.
LOCAL_MANUAL_MODE=true and ORDER_EXECUTION_ENABLED=false are mandatory.
Do not edit .env or serialize credentials.

Before public activation, publish this truthful customer notice through the
owner-approved channel route: "CCI V2 starts a fresh tracking generation.
Previously published signals are no longer monitored or updated by V2."
A notice receipt is required; it is not a trading signal or lifecycle receipt.
Never send a synthetic public signal.

After approved deployment, verify at least two naturally completed iterations,
measure actual database/WAL growth, refresh data coverage and resource status,
and inspect complete epoch/outbox ownership and delivery receipts. If there
is no eligible natural signal, report public signal delivery as NOT YET
OBSERVED. A correct NO TRADE remains acceptable; transport readiness alone
is not evidence that a natural public signal was delivered.
