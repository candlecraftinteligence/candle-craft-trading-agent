# L. Review packet

- Repository: `candlecraftinteligence/candle-craft-trading-agent`
- Base SHA: `962dbbb35db942beef0423c0763555e665da668d`
- Head SHA: `the commit that contains this packet`
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/136
- Tests: 3110 passed, 1 warning, Python 3.11.9, 2026-10-19, on the correction commit; this packet line does not change assertions
- CI: required GitHub check on this exact commit; an earlier green run does not cover it

Production paths touched: none. Scanner, config, schema, strategy, Telegram, and lifecycle code are unchanged.

Astra verdict on `cc44ae98168038f39fe4e27f0f7fe2b2ad73cb43`: REQUIRES_CORRECTION. This packet is the correction, not approval.

| id | resolution | remaining limit |
| --- | --- | --- |
| R1 | CMC-id crosswalk required; ticker-only matches stay unverified and unqueued. Settlement, quote, type, and multiplier are checked. Prior 100/74 claims withdrawn. | No retained crosswalk for the earlier live page, so that page is not re-certified. |
| R2 | Structural ranks and temporal validity are separate. Invalid ranking does not queue. Dominance and capitalization domains are checked. | Freshness window is the explicit six-hour research window, not a production allowance. |
| R3 | Provider blocks are isolated. Error envelopes are not observed comparisons. A partial summary is always written. | None for the injected cases. |
| R4 | Sampling writes normalized observations with checksums. Replay reads that artifact. Raw bodies are not claimed as stored. MCP connection is implementer-reported and independently unverified. | Sanitized MCP schemas were not in the repo. No new MCP call. |
| R5 | Deadline and byte limits apply during streaming. Retry-After accepts HTTP-date. Live sampling stays off. Metered calls stay off. | Shared-IP spare capacity remains unknown. |
| R6 | `depth_update_gap` removed. Tests use `SynchronizedLocalOrderBook`. | Production book code unchanged. |
| R7 | Funding and OI are traced through enrichment and `REJECTED_BY_DERIVATIVES`. Liquidation cases use `SymbolLiquidationAggregator`. | Source timestamps are still dropped before rejection. That gap is recorded, not patched. |
| R8 | Synthetic price, volume, OI, and funding comparison plus a configured-request benchmark. Provider matrix uses the 2026-10-19 documents. | Live metrics and Binance weight table remain unresolved. CoinGlass higher-plan prices are incomplete. |

Independent reviewer: Astra. This document is not Astra approval. A new head needs a new review.

Allowed next action after a verdict: only what the verdict names. No Runtime pull, restart, or database action is authorized by this phase.
