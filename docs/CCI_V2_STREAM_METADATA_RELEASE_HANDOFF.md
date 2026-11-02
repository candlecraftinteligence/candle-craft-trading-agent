# CCI V2 stream metadata lifecycle release handoff

## Candidate and scope

- Branch: `codex/cci-v2-stream-metadata-lifecycle`.
- Production baseline / compatible rollback: `1d163143ee99b14a1c5c7576ebddc1843c9b945f`.
- The PR description records the exact candidate SHA and CI receipts. No merge or deployment is authorized by this handoff.
- Scope: Binance public exchangeInfo cache, stream metadata maintenance, identity admission continuity, collector recovery, and deterministic tests.

## Demonstrated root cause

The deployed client caches exchangeInfo for 300 seconds. StreamEvidence independently rejects acquisition ages above 300 seconds. ScannerRunner previously distributed metadata only during a scan. A scan at age 299 therefore redistributed the acquisition at age zero. Another client consumer could renew exchangeInfo at age 301 while all three stream services still retained the old acquisition. Connected, acknowledged transports then rejected correctly identified, current market events as `exchange_metadata_stale_or_future`.

An additional recovery defect allowed quiet liquidation coverage to survive an unobserved metadata validity gap. Installing a fresh identity snapshot could immediately make the old coverage VERIFIED again.

Both regressions were run before implementation changes and failed. They were also rerun against an archived copy of the exact production baseline: **2 failed, 2 deselected**. The first reproduction starts three acknowledged, working in-memory streams and reports the stale metadata reason from all three services at second 301. The second demonstrates the incorrect VERIFIED liquidation result after a 901-second metadata gap. No market event freshness limit or invalid contract is responsible for those failures.

## Repair

- One cancellable maintainer belongs to the long-lived Binance HTTP client shared by scanner iterations. Scanner registration never acquires subscription or transport ownership.
- Renewal is due at source acquisition age 240 seconds, leaving 60 seconds of scheduling headroom under the unchanged 300-second admission limit. The upstream cache accepts a stricter maximum age so the request can actually acquire a new response.
- Cached reads retain their original authoritative acquisition timestamp and defensive payload copies. The source cache and refresh lifecycle are serialized. Duplicate scanner registrations share one task and one acquisition.
- Each refresh has a 10-second total timeout. Failures retry after 5, 10, 20, then at most 30 seconds. The existing HTTP client continues to enforce provider cooldowns, rate limits, response budgets, and retries.
- A network failure cannot extend the old source lease. Existing genuinely valid metadata remains usable only until its original expiry. Initial absence and malformed metadata fail closed; malformed snapshots are evicted so retries can acquire corrected data.
- Validated metadata is published synchronously to all three services without an intervening await. Exact Unicode identities and normalization collision rejection remain intact. Future, regressed, and conflicting acquisition chronology is rejected.
- Identical, continuously valid renewals preserve healthy coverage and books. Expiry or identity rejection invalidates affected coverage even when no event or assessment happened during the gap. Recovery starts fresh observation coverage and resynchronizes affected books while preserving connections and acknowledged subscriptions.
- Old cancelled bootstrap tasks cannot install into recovered books. Expired/future market observations and depth snapshot timestamps cannot contribute trusted evidence.
- Closing the HTTP owner cancels and joins renewal. Cancelling an individual scanner waiter leaves shared renewal owned by the client. Restart rechecks the source cache.

## Deterministic evidence

The 30-minute replay runs real collector receive loops against acknowledged in-memory transports and mocked public HTTP. It supplies all market events explicitly; it never inserts events into production or manufactures missing observations.

| Check | Result |
| --- | --- |
| Simulated interval | Seconds 0 through 1800 inclusive |
| Scanner registrations | Seven, at five-minute intervals |
| Genuine simulated source acquisitions | Eight: 0, 240, 480, 720, 960, 1200, 1440, 1680 |
| Contracts | BTCUSDT and literal Unicode 币安人生USDT |
| Aggregate trades | 1801 supplied and accepted per contract; no aggregate-ID gaps |
| Complete aggregate windows | VERIFIED from second 900 onward; 900 supplied events in each completed 15-minute window |
| Liquidations | BTC supplied once per minute; Unicode receives no liquidation events and reports a valid observed zero only after complete acknowledged coverage |
| Order books | Synchronized throughout; complete depth bands; two initial REST snapshots, no healthy renewal resyncs |
| Frozen book | Source time and values frozen at second 900; later events do not mutate it |
| Connections / subscriptions | One connection and one subscription request per service; no disconnects |

The outage replay acquires at second 960, fails renewal at 1200, withholds after expiry at 1261, and recovers at 1265. Books resynchronize once per affected symbol. Aggregate and quiet liquidation coverage remain withheld until the complete new canonical window `[1320, 2220)` is available. There are no additional connections or subscription requests.

Additional cases cover cache boundaries, 50 concurrent callers, duplicate registration, malformed responses and cache recovery, delayed response across expiry, cancellation and restart, scanner waiter cancellation, Unicode and normalization collisions, acquisition chronology, stale/future events and depth snapshots, aggregate-ID gaps, and telemetry persistence through combination, SQLite storage, hydration, briefing, and reporting.

Focused Windows Python 3.12.10 validation: **161 passed**. The local full suite completed with **4580 passed, 1 failed**: four hard-coded source inventory line references had moved. Those references and their generated inventory artifacts were updated, and all **34 inventory tests passed**. Compileall, pip check, and git diff check passed during development. The PR records final full-suite Windows and Linux CI results for its exact candidate SHA; a pending or failed check is not release approval.

## Safety and rollback

No strategy, risk, confirmation, Telegram outbox, runtime settings, database schema, migration, epoch, or provider selection code is changed. Preserve 1D/12H/4H/15M structure, Fibonacci 0.618/0.750/0.786, structural stops, net TP2 RR >= 3.0, Grade A/88, two confirmations, schema 27, CMC off, CoinPaprika ranking, local manual mode, and disabled order execution. Existing product and safety regression tests remain required.

No live database was accessed, copied, reset, or migrated. No scanner/listener process, real exchange probe, production Telegram message, live order, transfer, or deployment was run. Test databases are temporary. Test processes disable dotenv loading and enable the existing development safety flags.

Rollback requires the separately approved normal code rollback to the baseline SHA, followed by ordinary stream warm-up. There is no data migration to reverse. In-memory evidence is intentionally not persisted across process restart. Rollback restores the known metadata defect; it does not preserve this recovery behavior.

## Owner review and separately approved runtime acceptance

Production recovery remains **Unverified**. Tests cannot prove Binance scheduling, network latency, live source payload behavior, or Dev/Runtime clock alignment. After a separately approved rollout, the Runtime owner must observe genuine naturally received post-warm-up VERIFIED snapshots over complete 15-minute windows and consecutive five-minute scans, including metadata acquisition timestamps, stream acknowledgements, aggregate-ID continuity, book synchronization, and persisted stream health. Delayed responses, stalls, malformed identities, and actual validity gaps must withhold evidence. Never force signals, extend freshness, or synthesize Telegram messages for acceptance.

Stop for owner release review. Do not merge or deploy automatically.
