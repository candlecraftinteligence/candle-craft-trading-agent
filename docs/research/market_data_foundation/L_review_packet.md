# L. Review packet

- Repository: `candlecraftinteligence/candle-craft-trading-agent`
- Base SHA: `962dbbb35db942beef0423c0763555e665da668d`
- Head SHA: `the commit that contains this packet`
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/136
- Tests: 3116 passed, 1 warning, Python 3.11.9, 2026-10-19. Required CI on this commit is authoritative.
- CI: required GitHub check on this exact commit; an earlier green run does not cover it

Production paths touched: none. Scanner, config, schema, strategy, Telegram, and lifecycle code are unchanged.

Astra verdict on `a8c237529599089678e2029f51de9d2a40857313`: REQUIRES_CORRECTION for one remaining rate-limit hole. This packet stops a provider after its terminal HTTP 429. It is not approval.

| id | resolution | remaining limit |
| --- | --- | --- |
| R1 | CMC-id crosswalk required; ticker-only matches stay unverified and unqueued. Settlement, quote, type, and multiplier are checked. Prior 100/74 claims withdrawn. | No retained crosswalk for the earlier live page, so that page is not re-certified. |
| R2 / B1 | Structural completeness, numeric domain, and decision-time admissibility are separate. Invalid quotes and late receipt or source times do not queue, even with a valid crosswalk. | The six-hour window is a research bound, not a production allowance. |
| R3 / B2 | Transport, decode, parser, and domain results are stored on the observation. HTTP 200 is not verified evidence. A partial summary is still written. | None for the injected cases. |
| R4 / B3 | Replay reproduces normalized global metrics from checksummed observations and rejects failed, late, and schema-invalid rows. Raw bodies stay unretained. | MCP connection remains implementer-reported and independently unverified. No new MCP call. |
| R5 / B4 | Received bytes include discarded chunks and stop the study. A terminal HTTP 429 stops that provider for the rest of the finite run, including when retries are zero. Retry-After is honored in full or the retry is not attempted. Unrelated providers continue. Live sampling stays off. | Shared-IP spare capacity is unknown. CoinGlass stays disabled. |
| R6 | `depth_update_gap` removed. Tests use `SynchronizedLocalOrderBook`. | Production book code unchanged. |
| R7 | Funding and OI are traced through enrichment and `REJECTED_BY_DERIVATIVES`. Liquidation cases use `SymbolLiquidationAggregator`. | Source timestamps are still dropped before rejection. That gap is recorded, not patched. |
| R8 | Synthetic price, volume, OI, and funding comparison plus a configured-request benchmark. Provider matrix uses the 2026-10-19 documents. | Live metrics and Binance weight table remain unresolved. CoinGlass higher-plan prices are incomplete. |

Independent reviewer: Astra. This document is not Astra approval. A new head needs a new review.

Allowed next action after a verdict: only what the verdict names. No Runtime pull, restart, or database action is authorized by this phase.
