# CCI P0 — WebSocket runtime recovery

Date: 2026-10-26 (Europe/Bratislava). Status: DEV correction prepared; Runtime diagnosis and recovery acceptance pending manual evidence.

## Scope and repository handoff

- Expected Runtime release and repair base: `a7899eb3541ca8d08735771281789ab0dad45687` (local `origin/main`). Runtime HEAD has **not** been verified.
- Original DEV checkout: `C:\CandleCraftDev`, branch `feature/cmc-hybrid-active-intelligence`, HEAD `1cecd54c714bea24fdce5009a1c6f51f3e21e520`. Its untracked `CCI_ALPHA_RECOVERY_ROOT_CAUSE.md` was preserved.
- Isolated worktree: `C:\CandleCraftDev\.worktrees\p0-binance-wss`.
- Repair branch: `codex/p0-binance-wss-recovery`.
- Implementation commit: `1469ad2aaa19ab28e7a6784c1845cc8464cc68e6`. Draft PR: [#150](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/150). GitHub returned no PR-triggered CI runs at the last check on 2026-10-26; no CI pass is claimed.
- No production scanner/listener started, stopped, restarted, or signalled. No live database opened, copied, queried, or modified. No deployment or environment/firewall change performed.
- User confirmed Runtime checks must run manually on the separate PC. The Runtime directory does not exist on this connected DEV machine. DEV results cannot establish Runtime network or process health.

## Owner-directed hold and upstream overlap

After publishing the draft, GitHub reported that main had advanced to `235ec88c09ff6df0372bd636eaaf47814e48bd6d`, merged [PR #149](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/149), an overlapping order-book recovery repair. That change already addresses order-book flapping backoff / pre-cleanup invalidation and adds a 20 second opening timeout, 30 second receive timeout, phase/retry diagnostics and tests. It does not establish a Runtime root cause. The expected incident release and this draft's base remain `a7899eb`.

The owner explicitly directed: **“Keep #150 as a draft; diagnostics only.”** No reconciliation, merge, rebase, further implementation change or deployment will be performed under this handoff. PR #150 is reported as not mergeable. Do not deploy it as-is: future reconciliation must preserve #149's safeguards and tests, and needs renewed owner authorization. This report distinguishes findings against the expected release from code now present on upstream main. Runtime's actual installed commit is still N/A.

## Confirmed defects versus incident uncertainty

The exact mechanism behind the **Runtime opening-handshake timeouts and abrupt transport losses remains unconfirmed**. A receive-side failure cannot explain an opening-handshake timeout by itself. An error without a close frame does not identify whether Binance, a middlebox, the local network, or the event loop caused the loss.

The following code defects were reproduced / protected by DEV regressions and repaired:

1. Every feed reset its reconnect failure counter immediately after sending subscriptions. Repeated successful handshakes followed by immediate receive failures therefore continually used the shortest retry interval. The common policy now preserves failures across flapping connections, exponentially increases delay, applies 20% jitter (including at the ceiling), and resets only after an accepted market event on a connection at least 60 seconds old. Defaults: 0.8–1, 1.6–2, 3.2–4 seconds, progressing to a 24–30 second ceiling. This is reconnect control, not evidence of complete market coverage.
2. Feed invalidation previously occurred after awaited socket cleanup (and, for books, bootstrap cancellation). A closed socket could leave data trusted during cleanup. Invalidation now happens before those waits, including explicit service shutdown. The connection task remains owned during shutdown to prevent a second start while socket closure is pending.
3. The first depth bridge applied updates and declared synchronization without checking that both book sides remained nonempty and uncrossed. Deleting a complete side could cause a later `max()` error; a crossed initial book could also transiently appear synchronized. The bridge now uses the same fail-closed nonempty/uncrossed validation as live updates.
4. Subscription responses with `code` were silently counted as ignored control messages. They now raise a sanitized subscription error and enter the normal invalidation/backoff path. ACKs still do not count as market progress.
5. Aggregate-trade transport passed `ping_interval=None, ping_timeout=None`, disabling active heartbeat failure detection. Its default now matches the order-book and liquidation clients at 20/20 seconds. Server Ping responses remain the library's responsibility.

These defects are **not asserted to be the cause of the reported Runtime handshake failures**. No timeout was increased. No speculative URL replacement or automatic proxy bypass was introduced.

## Actual endpoints and current official protocol

| Feed | Old release URL | New release URL | Subscription |
| --- | --- | --- | --- |
| Order book | `wss://fstream.binance.com/public/stream` | unchanged | lowercase `<symbol>@depth@500ms` via JSON SUBSCRIBE |
| Aggregate trades | `wss://fstream.binance.com/market/stream` | unchanged | lowercase `<symbol>@aggTrade` via JSON SUBSCRIBE |
| Liquidations | `wss://fstream.binance.com/market/stream` | unchanged | `!forceOrder@arr` via JSON SUBSCRIBE |

The current official depth reference maps diff depth to `/public/ws/{symbol}@depth@{updateSpeed}` and `/public/stream?streams=...`. The migration notice explicitly places depth in the public category. Therefore this repair does **not** move depth to the incident brief's `/market/ws/...@depth` example. The existing CCI depth route matches current official guidance. [Binance depth reference](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public), [migration notice](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice).

Trade and liquidation raw routes use `/market/ws/...`; combined URL routes use `/market/stream?streams=...`; live subscriptions use `/market/stream`. Raw and `{stream,data}` envelopes are decoded by the existing parsers. The merged all-market liquidation feed requires `st=1` for USD-M; `st=2` is COIN-M and is rejected. `ps` is the pair symbol, not a substitute for contract type or perpetual identity. The probe independently checks symbols against public exchangeInfo `TRADING`, `PERPETUAL`, `quoteAsset=USDT` metadata. [Binance market reference](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market).

Connections expire at 24 hours; official limits include 1024 streams/socket and 10 incoming messages/second. Binance documents a server Ping every 3 minutes and a 10 minute Pong response window. CCI uses the library's protocol response plus active heartbeats. [Binance connect rules](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams).

JSON live SUBSCRIBE/UNSUBSCRIBE uses lowercase stream names, incrementing integer request IDs and ACKs with `result:null`. The official live-subscription page documents these IDs, although generated API examples also show string IDs; CCI retains its existing documented integer format. [Binance live subscriptions](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Live-Subscribing-Unsubscribing-to-streams).

DEV uses Python 3.11.9 and websockets 16.0; requirements permit `>=15.0,<17.0`. Opening timeout is 10 seconds, closing timeout 5 seconds. Depth is bounded to 1 MiB/message and 4 queued frames; trades/liquidations to 64 KiB/message and 32 queued frames. Compression remains the library default. websockets 15+ can use system proxies automatically; proxy credentials/addresses must not be logged. No dependency upgrade was made. [websockets 16 client reference](https://websockets.readthedocs.io/en/16.0/reference/asyncio/client.html).

## Order-book integrity audit

- One watch-owned depth task/socket; subscriptions change via a send lock. Bootstrap tasks are per-symbol, cancelled on disconnect/removal; fresh snapshots are required after reconnect.
- Snapshots use credential-free `https://fapi.binance.com/fapi/v1/depth`, limit 500, 10 second HTTP timeout, concurrency 2, minimum snapshot start spacing 0.5 seconds. HTTP 418/429, Retry-After, timeouts, invalid JSON and malformed snapshots are classified. Recovery remains paced and cancellable.
- Per-symbol event buffer: 256 events by default. Overflow invalidates state and resynchronizes. There is no unbounded raw-event queue.
- Initial bridge retains the official Futures rule `U <= snapshot.lastUpdateId <= u`, dropping only events with `u < lastUpdateId`. Later frames require `pu == previous u`. Sequence gaps invalidate; older/duplicate frames do not refresh freshness.
- The pre-existing untracked alpha report's proposed contiguous-only bridge exception was **not** implemented: current official instructions still require snapshot overlap. Accepting `U == lastUpdateId + 1` would relax that documented condition.
- Freshness uses source event time; stale books cannot be VERIFIED. Incomplete depth coverage does not become a valid liquidity band. The new initial-side validation closes the empty/crossed bridge defect.
- Actual Runtime snapshot bridging, valid book counts and resynchronization duration: **N/A**, awaiting Runtime output.

[Official Futures local-book sequence procedure](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly).

## Microstructure and liquidation audit

Trade and liquidation clients are separate watch-owned sockets using the market route, each with bounded retention. Existing parsers require post-migration USD-M discrimination; subscriptions/retained symbols come from the scanner's instrument universe. Liquidations retain exchange event/trade timestamps and receipt timestamps, fingerprint deduplicate within bounded storage, and invalidate coverage on disconnect or stale arrival. Reconnect cannot erase a missing interval or fabricate a zero-liquidation observation. Binance's liquidation stream is a latest-per-symbol snapshot per 1000 ms, not an exhaustive fill tape. Absence of a liquidation frame in a short probe is inconclusive, even when aggregate-trade frames are healthy.

## Runtime evidence ledger

| Required evidence | Result |
| --- | --- |
| Runtime commit, Python/websockets version | N/A — manual output pending |
| DNS / TCP 443 / validated TLS | N/A — not measured on Runtime |
| HTTP upgrade / negotiated protocol | N/A — not measured on Runtime |
| Order-book / trade / liquidation connection status | Unverified |
| Actual valid market frames by type | N/A; loopback fixture frames are not market observations |
| Valid / stale / unavailable symbol counts | N/A |
| Actual snapshot synchronization | N/A |
| Ping RTT, disconnect codes/frequency | N/A |
| Reconnect timing/concurrency on Runtime | N/A |
| CPU / RAM pressure, duplicate Runtime processes | N/A |
| Runtime scanner event-loop lag | N/A; external counters/probe loop lag cannot measure it |
| Proxy/firewall state | N/A; no settings changed |
| Consecutive scanner cycles after correction | 0 observed |

## Manual read-only diagnostics — can run before deployment

Copy only `scripts/diagnose_binance_websocket.py` and `scripts/collect_websocket_host_health.ps1` to `%TEMP%\cci-p0` on Runtime. They can use the existing expected release's parsers from the current Runtime working directory; copying these standalone diagnostics does not deploy the repair or edit tracked Runtime source.

```powershell
Set-Location 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
git status --short
git rev-parse HEAD
& .\.venv\Scripts\python.exe "$env:TEMP\cci-p0\diagnose_binance_websocket.py" --seconds 15
& "$env:TEMP\cci-p0\collect_websocket_host_health.ps1"
```

The Python probe makes one bounded DNS check, direct TCP and validated TLS checks, one public instrument-metadata request and two sequential WebSocket probes: depth and combined BTC aggregate-trade/all-market liquidation. It requests one depth snapshot without retries. Maximum sockets concurrently: one diagnostic WebSocket plus one HTTP snapshot request; no scanner or listener. Receive windows are capped at 30 seconds; open and network stages are also bounded. Only public data is accessed. Outputs omit payloads, exception bodies, headers, process arguments and proxy/environment values. Source-time ranges, actual counts, subscription ACK, Ping RTT, HTTP status, sequence gaps and end-of-window book freshness are retained.

If default proxy mode fails while direct DNS/TCP/TLS pass, one explicit comparison can use `--direct`; this affects only the diagnostic process. Do not change system proxy, certificates or firewall until results isolate the failure. A TLS validation failure must be corrected through the host trust configuration, never by disabling validation.

`--reconnect-test` closes/recreates only the diagnostic depth socket once after a 2 second pause. It does not disconnect CCI. It proves fresh connection/snapshot bridging in the probe; application reconnect acceptance still requires Runtime observation and approved fault testing. Use at most one initial run and one evidence-driven comparison, avoiding reconnect storms. No test can prove provider intent from a missing close frame.

## Scanner performance and independent incident classification

About 94 watched, 2 evaluated, 14 warnings, PARTIAL, ~405 seconds/cycle and ~600 second cadence are supplied observations, not independently measured here. There is no evidence tying every warning to WebSockets.

`ScannerRunner` processes symbols sequentially and can perform concurrent optional REST calls within a symbol. Snapshot HTTP I/O is asynchronous and bounded. Synchronous strategy/technical analysis runs on the same event loop (`scanner_runner.py`), as does synchronous primary scan persistence through `profile.call("scan_storage", ...)` in `run_scan.py`. These are plausible starvation sources, especially on a large database, but duration/lag correlation is unavailable. No production query, profiling injection or storage rewrite was performed.

The existing feed phase `SUCCESS` after subscription reconciliation means the reconciliation call returned, not that data is fresh. Feed snapshots and actual frame/book counters must be evaluated separately. Preserve the existing N/A / Unverified diagnostics and fail-closed strategy gates.

Classify remaining warnings separately using at least five consecutive scanner-cycle summaries: discovery/universe, request/symbol/global timeout, optional provider failure, candle freshness, microstructure stale/unavailable, snapshot bootstrap, storage/reporting, Telegram/outbox/provenance, and schedule overruns. Retain per-phase timings, outcome counts (evaluated/rejected/timed_out/not_run), expected versus actual starts and skipped intervals. A lack of setups alone is not a failure. Do not tune trading gates to increase evaluated counts.

## Changes and safety boundaries

Modified only the three feed services, shared retry policy, initial-book validation, focused tests, standalone diagnostic/report files and one research-catalog source line reference shifted by the added import. RR, grades, scoring, confirmation, lifecycle identity, CMC rules, Telegram identity and database schema are unchanged. No execution, transfer or withdrawal client was added. Retry jitter affects transport timing only, never deterministic trading signals.

## Verification

- Project Python: `C:\CandleCraftDev\.venv\Scripts\python.exe` (existing environment).
- Focused suite: **123 passed in 9.91 seconds**. Includes actual loopback HTTP 101, 404 rejection, opening timeout, Ping/Pong, provider close 1012, oversized frame bounds, repeated receive/handshake failures, exponential retry/jitter bounds, cancellation, duplicate-start protection, shutdown/restart, rejected subscriptions, raw/combined USD-M and COIN-M parsing, snapshot bridge, sequence gap, overflow, stale and empty/crossed book handling, plus sanitized diagnostic tests.
- Loopback diagnostic integration received one fixture depth frame and synchronized a fixture snapshot. This is protocol/test evidence only, not a Binance market frame count.
- Full DEV regression completed: **3713 passed, 1 failed, 1 warning in 668.81 seconds**. The sole failure was `test_inventory_anchors_match_source`: the added import shifted its source anchor from line 92 to 93. The reference was corrected in the implementation commit; the unchanged assertion then passed separately (**1 passed in 0.48 seconds**). The in-flight full process had loaded the old catalog before the correction. There is no clean all-pass full run or CI result for this draft, and no validation against current main is claimed. The warning is an existing Starlette/httpx deprecation.
- Dependency check: PASS (`No broken requirements found`). Changed Python compile check: PASS. PowerShell diagnostic syntax: PASS. Tracked working diff check: PASS. The initial implementation commit has a trailing blank line warning in the new recovery test file; a local whitespace-only cleanup was prepared before the owner hold and is not an implementation change or a CI pass.
- Initial sandboxed asyncio tests stalled while Windows created its loopback socketpair; they were interrupted. Tests then ran with permitted local sockets and the four DEV safety flags. An earlier full run was interrupted to incorporate additional repairs/tests; it is not counted as a pass.
- All test databases are isolated under this worktree's pytest temporary directories. No full scanner/listener is launched by this work.

## Deployment and restart requirements

1. PR #150 remains draft by owner direction. Future reconciliation with PR #149 requires renewed owner authorization, followed by a clean full regression run. Review the reconciled PR and wait for passing required GitHub CI. Owner approval is required for merge and separately for deployment/restart. No merge or deploy has occurred.
2. Obtain Runtime commit, host/probe output and sanitized scanner-cycle evidence. Verify the source checkout is clean without discarding any changes. Confirm the launch owner, environment and existing scanner/listener counts.
3. If diagnostics isolate a host/network fault, prefer the smallest owner-approved host correction and repeat bounded probes. Do not deploy a code release as a substitute for missing network evidence.
4. A code fix affects newly started Python processes. After the approved merge, deploy the reviewed merge SHA using the existing controlled release procedure; do not check out this feature branch directly into a running Runtime checkout. No schema migration or database operation is needed by this fix.
5. A scanner restart is needed to load changed feed code; a Telegram listener restart is not required by these files. Perform it only after explicit operational approval and with verified original launch parameters, one instance, and existing runtime-identity/epoch safeguards.
6. Exact scanner stop/start commands: **N/A until the actual Runtime launch method and parameters are supplied**. No authoritative launcher exists in the inspected checkout; README examples are insufficient to reconstruct production flags, database path, Telegram identity or runtime epoch. Do not run a guessed `run_scan.py --watch` command or a broad `Stop-Process python`. Preserve the original owner-controlled launcher. This is an operational handoff requirement, not permission to restart.

## Bounded recovery acceptance after approved correction

Observe five consecutive complete scanner cycles (about 50 minutes at the reported cadence), recording cycle outcomes, warnings, CPU/RAM samples, subscription/valid-frame counts, book synchronization/freshness, gap/resync counters and reconnect intervals. Continue until at least one approved controlled disconnect/recovery is verified against the actual service, with no parallel scanner/listener. A diagnostic-only socket recycle is insufficient. Do not inject a disconnect into production without separate approval.

Require HTTP 101 on correct routed URLs, actual st=1 USDT-perpetual traffic, a snapshot-overlap bridge, continuous pu/u sequence, fresh VERIFIED books, unverified/unavailable state throughout disconnection, fresh synchronization on reconnect, bounded attempts, stable resource use and scanner responsiveness. Preserve missing coverage in microstructure windows; successful reconnection alone cannot restore historical coverage. Observe liquidation traffic when it actually exists; report quiet periods accurately. Five cycles do not establish 24-hour rotation reliability; retain next-day observation through a natural connection expiry. Keep PARTIAL/Telegram/performance findings open independently.

## Runtime acceptance verdicts

The required binary values below are conservative **acceptance-not-demonstrated** results, not claims that unperformed Runtime tests observed network/data corruption.

```text
WEBSOCKET_HANDSHAKE: FAIL
ORDER_BOOK_SYNCHRONIZED: NO
MICROSTRUCTURE_RECEIVING: NO
RECONNECT_RECOVERY: FAIL
DATA_INTEGRITY: FAIL
SCANNER_PARTIAL_ROOT_CAUSE: UNCONFIRMED
RUNTIME_FIX_APPLIED: NO
READY_FOR_NORMAL_OPERATION: NO
```

Runtime recovery remains open pending manual diagnostics, reviewed correction approval and consecutive-cycle acceptance. DEV tests cannot close this P0 incident. The immediate next action is to run the pinned standalone probes below and return their sanitized output, not deploy draft #150.

## One-block Runtime diagnostic handoff

This downloads only the two diagnostic files from the immutable implementation commit into the temporary directory, without updating Runtime source:

```powershell
$ErrorActionPreference = 'Stop'
$cciDiagFolder = Join-Path $env:TEMP 'cci-p0'
New-Item -ItemType Directory -Path $cciDiagFolder -Force | Out-Null
$cciSource = 'https://raw.githubusercontent.com/candlecraftinteligence/candle-craft-trading-agent/1469ad2aaa19ab28e7a6784c1845cc8464cc68e6/scripts'
Invoke-WebRequest "$cciSource/diagnose_binance_websocket.py" -OutFile "$cciDiagFolder\diagnose_binance_websocket.py"
Invoke-WebRequest "$cciSource/collect_websocket_host_health.ps1" -OutFile "$cciDiagFolder\collect_websocket_host_health.ps1"
Set-Location 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
git rev-parse HEAD
& .\.venv\Scripts\python.exe "$cciDiagFolder\diagnose_binance_websocket.py" --seconds 15
& "$cciDiagFolder\collect_websocket_host_health.ps1"
```

Committed diagnostic SHA-256 hashes (LF source bytes):

- `diagnose_binance_websocket.py`: `6b204a9f3ffec76a3a1ddbd617547020d98d41cb2829d3da38e9d4d4f4efdc07`
- `collect_websocket_host_health.ps1`: `f4b0fc928cda5c0ba6d67d135fd586325e945032da30422a4795130b96b8a4ce`

These scripts were prepared before the owner hold. They do not start or stop the scanner/listener. If the Runtime interpreter path differs, use its established Python environment rather than installing dependencies or changing production configuration.
