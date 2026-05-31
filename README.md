# Candle Craft Trading Agent

Phase 1 foundation for a crypto trading intelligence system, with Phase 2 public market-data clients, Phase 3 technical structure analysis, Phase 4 derivatives/orderflow context analysis, Phase 5 risk-management validation, Phase 6 opportunity scoring, Phase 7 structured trade ideas, Phase 8 dry-run-first alert formatting, Phase 9 in-memory journal tracking, Phase 10 scanner-runner orchestration, Phase 11 liquidity-grab pullback strategy analysis, Phase 12 scanner strategy integration, Phase 12.1 multi-timeframe scanner context, Phase 12.2 confirmation timeframe diagnostics, Phase 13 candle-estimated Volume Profile / POC context, Phase 14 refined OB/FVG plus fib pullback-zone validation, Phase 15 public derivatives enrichment, Phase 15.2 multi-timeframe confirmation-to-pullback integration, Phase 16 Telegram-ready scanner formatting, Phase 17 premium scanner display output, Phase 18 scanner result ranking, Phase 19 watchlist presets, Phase 20 batch-scan reliability, Phase 21 public symbol universes, Phase 22 near-miss intelligence, Phase 23 setup quality validation, Phase 24 historical replay validation, Phase 28 portfolio selection, Phase 29 alert watch mode, Phase 31 adaptive market regime filtering, Phase 32 performance memory, Phase 33 structured scan history storage, Phase 34 research analytics queries, Phase 35 regime intelligence and environment filtering, Phase 36 setup lifecycle state progression, Phase 37 lifecycle conversion analytics, Phase 38 pullback structure intelligence, Phase 39 adaptive symbol prioritization, Phase 40 graceful watch shutdown with watch iteration persistence, Phase 41 wick-vs-close structural intelligence, Phase 42 dynamic RR and target intelligence, Phase 43 local runtime and persistence hardening, Phase 44A scan persistence integrity auditing, Phase 44B lifecycle replay readiness auditing, Phase 44C replay dataset export contract, and Phase 44D replay dataset quality metrics.

This project is intentionally not an auto-trading bot. It does not place orders, does not expose exchange trading endpoints, and does not include withdrawal or transfer functionality. The initial scope is a modular backend foundation for market data, technical features, catalysts, trade ideas, alerts, manual or paper trade records, journal entries, and backtest metadata.

## Stack

- Python 3.11+
- FastAPI
- PostgreSQL
- SQLAlchemy 2.x
- Alembic
- Pydantic settings
- pytest
- async httpx
- Docker Compose

## Project Structure

```text
.
|-- alembic/
|-- app/
|   |-- agents/
|   |-- analytics/
|   |-- alerts/
|   |-- api/
|   |-- backtesting/
|   |-- cache/
|   |-- core/
|   |-- data/
|   |   |-- exchange_clients/
|   |   `-- normalizers/
|   |-- db/
|   |-- formatters/
|   |-- models/
|   |-- pipeline/
|   |-- scoring/
|   |-- storage/
|   |-- strategies/
|   `-- watchlists/
|-- scripts/
|-- tests/
|-- docker-compose.yml
|-- requirements.txt
|-- pytest.ini
|-- .env.example
|-- AGENTS.md
`-- README.md
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Replace the placeholder values in `.env` before using a shared or persistent environment.

Start PostgreSQL:

```powershell
docker compose up -d postgres
```

Run the API:

```powershell
uvicorn app.main:app --reload
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Database

The SQLAlchemy models are in `app/models`. Alembic is configured to read `DATABASE_URL` through the app settings loader.

Create a migration after model changes:

```powershell
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests cover settings loading, the FastAPI health endpoint, model metadata imports, mocked public market-data client responses, deterministic analysis agents, risk validation, opportunity scoring, structured trade idea generation, mocked alert delivery behavior, in-memory journal tracking, the Phase 10 scanner runner, the Phase 11 liquidity-grab pullback engine, the Phase 12 scanner strategy integration, the Phase 12.1 synthetic 2D timeframe model, the Phase 13 candle-estimated volume profile, the Phase 14 pullback-zone engine, the Phase 15 derivatives enrichment layer, the Phase 15.2 confirmation-to-pullback integration, the Phase 16 Telegram-ready formatter, the Phase 17 premium scanner display formatter, the Phase 18 scanner result ranking layer, the Phase 19 watchlist preset resolver, the Phase 20 cache/resume reliability layer, the Phase 21 symbol universe layer, the Phase 22 near-miss intelligence layer, the Phase 23 setup quality layer, the Phase 24 historical replay layer, the Phase 28 portfolio selection layer, the Phase 29 alert watch mode, the Phase 31 market regime filter, the Phase 32 performance memory layer, the Phase 33 scan history database, the Phase 34 research query layer, the Phase 35 regime intelligence layer, the Phase 36 lifecycle engine, the Phase 38 pullback intelligence layer, the Phase 39 symbol health layer, the Phase 40 watch persistence and shutdown layer, the Phase 41 wick-vs-close structural intelligence layer, the Phase 42 target intelligence layer, the Phase 43 local runtime diagnostics, the Phase 44A scan persistence audit, the Phase 44B lifecycle replay readiness audit, the Phase 44C replay dataset export contract, and the Phase 44D replay dataset quality metrics. Tests do not call live exchange APIs or live Telegram APIs.

## Phase 43 - Local Runtime & Persistence Hardening

Run the local runtime health check:

```powershell
.\.venv\Scripts\python.exe scripts\check_local_runtime.py
```

JSON mode:

```powershell
.\.venv\Scripts\python.exe scripts\check_local_runtime.py --json
```

The check validates Python 3.11+, virtual environment detection, project-local pytest temp/cache writability, `.env` presence, masked config key readiness, PostgreSQL config shape without connecting, Docker CLI/daemon readiness, and generated scan artifact hygiene.

Docker readiness warnings are local environment diagnostics. Docker Desktop being stopped, unavailable, or permission-denied does not mean the trading system failed and does not block non-Docker diagnostics.

Generated scan JSON files such as `scan_runs/*.json` and `scan_output.json` are local artifacts and should not be staged or committed. If a generated scan artifact is already tracked, restore or untrack it separately rather than mixing its generated diff into a feature phase.

## Phase 44A - Scan Persistence Integrity Audit

Phase 44A adds a deterministic, read-only audit layer for local scan persistence artifacts. It inspects scanner run JSON, watch-state-like JSON, and performance-memory-like JSON for internal consistency, readable structure, basic schema stability, and later lifecycle replay readiness. It does not execute replay, call exchanges, send Telegram messages, place orders, alter lifecycle transitions, or change scanner, setup, scoring, regime, risk, portfolio, or alert behavior.

Run the audit with default local paths:

```powershell
.\.venv\Scripts\python.exe scripts\audit_scan_persistence.py
```

JSON mode:

```powershell
.\.venv\Scripts\python.exe scripts\audit_scan_persistence.py --json
```

Audit explicit artifacts:

```powershell
.\.venv\Scripts\python.exe scripts\audit_scan_persistence.py scan_output.json scan_runs\latest_scan.json
```

If no paths are provided, the script checks `scan_output.json` when present and all `scan_runs/*.json` files when present. It prints metadata, severity counts, inspected fields, and issue messages only; it does not print full artifact contents or secrets.

Warnings mean the artifact is readable but incomplete, unusual, or not fully ready for research/lifecycle replay validation. Errors are reserved for unreadable or invalid JSON and similarly blocking structural problems. The CLI exits `0` when there are warnings but no errors, and exits `1` when any audited artifact has an error.

Generated local artifacts such as `scan_output.json`, `scan_runs/latest_scan.json`, `scan_runs/watch_state.json`, and `scan_runs/performance_memory.json` remain ignored local data and should not be committed.

## Phase 44B - Lifecycle Replay Readiness Audit

Phase 44B adds a deterministic, read-only lifecycle replay readiness audit. It inspects lifecycle-like records from local scanner, watch-state, and performance-memory JSON artifacts and reports whether their statuses, status history, identifiers, timestamps, invalidation fields, and outcome fields are ready for future replay validation.

This audit does not mutate lifecycle states, watch state, scan history, performance memory, scanner results, trade ideas, alerts, or database records. It does not execute trades, place orders, call exchanges, send Telegram messages, invent market data, or alter setup, scanner, scoring, regime, risk, portfolio, lifecycle, or alert gates.

Run the lifecycle replay audit with default local paths:

```powershell
.\.venv\Scripts\python.exe scripts\audit_lifecycle_replay.py
```

JSON mode:

```powershell
.\.venv\Scripts\python.exe scripts\audit_lifecycle_replay.py --json
```

Strict mode:

```powershell
.\.venv\Scripts\python.exe scripts\audit_lifecycle_replay.py --strict
```

If no paths are provided, the script checks `scan_output.json`, `scan_runs/latest_scan.json`, `scan_runs/watch_state.json`, and `scan_runs/performance_memory.json` when those files are present. Explicit JSON paths can also be passed on the command line.

Warnings mean the artifact is readable but has incomplete replay readiness, unknown statuses, suspicious transitions, missing identifiers, missing timestamps, missing invalidation/stop fields, or missing terminal outcome fields. Errors are reserved for malformed lifecycle structure that blocks the audit, invalid JSON, unreadable files, and `status_history` fields that are not JSON arrays. The CLI exits `0` when there are warnings but no errors, exits `1` when errors are found, and exits `1` in `--strict` mode when warnings or errors are found.

The audit prepares local artifacts for future replay validation. It does not enforce a production state machine, execute trades, or loosen any existing setup gates.

## Phase 44C - Replay Dataset Export Contract

Phase 44C adds a deterministic, read-only replay dataset export layer. It converts existing local scanner, watch-state, lifecycle, and performance-memory JSON artifacts into normalized replay dataset rows for future historical validation and research analytics.

This is research/export-only. It does not execute replay, create signals, place trades, call exchanges, send Telegram messages, change setup gates, mutate lifecycle state, alter scanner results, update performance memory, or invent missing market data. Missing values are exported as `N/A`, empty lists, `false`, or zero as appropriate, and incomplete replay readiness is reported as warnings.

Run a dry-run export summary with default local artifacts:

```powershell
.\.venv\Scripts\python.exe scripts\export_replay_dataset.py --dry-run
```

JSON summary mode:

```powershell
.\.venv\Scripts\python.exe scripts\export_replay_dataset.py --json-summary
```

Export explicit scanner output as JSONL:

```powershell
.\.venv\Scripts\python.exe scripts\export_replay_dataset.py --input scan_runs\latest_scan.json --output replay_exports\latest_replay_dataset.jsonl --format jsonl
```

Export explicit scanner output as CSV:

```powershell
.\.venv\Scripts\python.exe scripts\export_replay_dataset.py --input scan_runs\latest_scan.json --output replay_exports\latest_replay_dataset.csv --format csv
```

Generated `replay_exports/*.jsonl`, `replay_exports/*.csv`, and `replay_exports/*.json` files are local research artifacts and should not be committed.

## Phase 44D - Replay Dataset Quality Metrics

Phase 44D adds deterministic, read-only quality metrics over the Phase 44C replay dataset contract. It measures replay readiness, field completeness, status and lifecycle coverage, no-setup distribution, trade idea/alert/journal presence, missing and unverified data counts, warning patterns, duplicate row identities, and terminal outcome coverage for research preparation.

This is audit/research-only. It does not execute replay, create signals, place trades, call exchanges, send Telegram messages, mutate artifacts, update scanner results, change lifecycle states, write performance memory, alter database records, or change setup gates. Missing values remain `N/A`, unreliable values remain `Unverified`, and market data is never invented.

Run quality metrics with default local artifacts:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_replay_dataset_quality.py
```

JSON mode:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_replay_dataset_quality.py --json
```

Strict mode:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_replay_dataset_quality.py --strict
```

Analyze an exported JSONL replay dataset:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_replay_dataset_quality.py --input replay_exports\latest_replay_dataset.jsonl --input-format jsonl --json
```

The `quality_score` is a 0-100 data/replay-readiness score. It is not profitability, not signal quality, and not setup confidence. Rejected and no-setup rows are valid research rows; they help measure dataset coverage and are not treated as failures merely because no trade idea was created.

The analyzer reports warnings for research-readiness limitations such as low replay readiness, missing symbols, missing timestamps, missing stable identifiers, missing status or lifecycle coverage, duplicate row identities, excessive `N/A` in critical fields, and terminal rows without `result_r` or `outcome_status`. Errors are reserved for unreadable files, invalid JSONL input, or row structures that prevent metrics.

## Phase 2 Market Data

Phase 2 adds read-only public market-data clients under `app/data`:

- `BinanceFuturesClient` for Binance USD-M Futures public market data.
- `BybitLinearClient` for Bybit Linear Perpetual public market data.
- Shared DTOs: `CandleDTO`, `TickerDTO`, `FundingDTO`, and `OpenInterestDTO`.

The clients use public GET endpoints only. They do not use API keys, account endpoints, private data, order placement, withdrawals, transfers, or trading functionality.

Normalized DTOs expose a consistent internal shape across Binance and Bybit. Missing optional fields are marked as `N/A`; malformed or incomplete required data raises a clear exchange client error.

Manual live check:

```powershell
.\.venv\Scripts\python.exe scripts\test_market_data_clients.py
```

The manual script fetches `BTCUSDT` candles, ticker, funding, and open interest from Binance and Bybit, then prints normalized DTOs. It is optional and not used by automated tests.

## Phase 3 Technical Structure Agent

Phase 3 adds the first deterministic analysis agent under `app/agents`:

- `TechnicalStructureAgent` analyzes already-collected OHLCV candles only.
- It does not call live APIs, use private exchange access, place orders, or produce trading recommendations.
- It returns structured DTOs such as `TechnicalStructureResult`, `SwingPoint`, `SweepSignal`, `BosSignal`, `ChochSignal`, and `VolumeAnomalySignal`.
- It calculates ATR, EMA 50, EMA 200, volume z-score, trend context, recent range high/low, nearest support, and nearest resistance.
- It detects confirmed swing highs/lows, bullish and bearish liquidity sweeps, simple BOS, and simple CHoCH using deterministic rules.
- Missing volume marks volume anomaly fields as `N/A`; malformed or insufficient OHLC data returns an invalid result with clear errors.

## Phase 4 Derivatives / Orderflow Agent

Phase 4 adds `DerivativesOrderflowAgent` under `app/agents` for deterministic derivatives context analysis:

- It analyzes normalized in-memory data only: price change, current and previous open interest, funding rate, optional historical funding rates, and optional volume z-score.
- It returns structured models including `DerivativesOrderflowResult`, `FundingSignal`, `OpenInterestSignal`, `PriceOiRelationship`, `CrowdingRiskSignal`, and `DerivativesDataQuality`.
- It classifies funding direction and severity, calculates funding z-score when historical funding is supplied, calculates OI percentage change, classifies price/OI relationships, detects crowded long or crowded short risk, and scores derivatives context from 0 to 100.
- Missing funding, open interest, or volume is marked as `N/A` and surfaced through explicit risk flags.
- CVD and liquidation heatmaps are currently not implemented and are always marked `N/A`; the agent does not invent either data source.
- The agent does not call live APIs, use private exchange access, produce trading recommendations, place orders, or expose trading execution.

## Phase 5 Risk Manager Agent

Phase 5 adds `RiskManagerAgent` under `app/agents` for deterministic risk validation of a proposed setup:

- It validates already-supplied setup data only: account equity, risk per trade, entry, stop loss, take profit targets, direction, optional leverage, optional daily risk limits, optional data quality score, and invalidation reason.
- It does not place trades, route orders, call exchanges, use private API access, or perform live execution.
- It calculates risk amount, stop-loss-based position size, notional value, and risk/reward for each take profit target.
- It rejects setups with missing invalidation, wrong-side stops, risk per trade above 2%, best risk/reward below 2.0, poor data quality, or exceeded daily risk limits.
- Missing optional values are marked as `N/A`; unreliable low data quality is marked `Unverified`.
- Leverage is never encouraged. If leverage is missing it is marked `N/A`; if leverage is supplied the result includes a risk warning, with higher tiers marked as high, extreme, or dangerous risk.
- Exact liquidation distance is currently `N/A` because exact liquidation price requires exchange-specific margin model and position settings.

## Phase 6 Opportunity Scoring Engine

Phase 6 adds `OpportunityScoringEngine` under `app/scoring` for deterministic scoring of candidate setups:

- It combines technical structure score, derivatives/orderflow score, risk-manager approval, best risk/reward, liquidity placeholder score, catalyst placeholder score, and data quality score.
- It scores candidate setups only. It does not create trade ideas, place trades, route orders, call private exchange APIs, or perform live execution.
- Missing liquidity defaults to 50 and is marked `N/A`; missing catalyst defaults to 0 and is marked `N/A`; missing data quality defaults to 50 and is marked `N/A`.
- Unverified input data is preserved in the output as `Unverified`; low supplied data quality below 60 is also treated as `Unverified`.
- Setup location defaults to `unknown` when not supplied.

Scoring weights total 100 points:

- Technical structure: 30 points
- Derivatives/orderflow: 20 points
- Liquidity: 15 points
- Catalyst: 15 points
- Risk/reward: 15 points
- Data quality: 5 points

Each component score is expected from 0 to 100 and is converted to its weighted contribution. Risk/reward is derived from `best_rr`: below 2.0 is rejected, 2.0 to 2.99 is moderate, 3.0 to 4.99 is strong, and 5.0 or higher is excellent.

Hard filters reject weak setups when any of these are true:

- Risk manager approval is false.
- Invalidation is missing.
- Best risk/reward is below 2.0.
- Data quality score is below 60.
- Setup location is `middle`.
- Technical score is below 50.
- Derivatives/orderflow score is below 40.
- Any risk-manager rejection reason is present.

Grades:

- `A+`: 90 to 100
- `A`: 80 to 89
- `B`: 70 to 79
- `C`: 60 to 69
- `Reject`: below 60 or any hard filter failure

Decisions:

- `high_quality_candidate`: score 90 or higher with no hard rejects
- `alert_candidate`: score 80 or higher with no hard rejects
- `watchlist_only`: score 70 or higher with no hard rejects
- `reject`: any hard reject, or any score below 70

## Phase 7 Trade Idea Agent

Phase 7 adds `TradeIdeaAgent` under `app/agents` for turning an already-scored candidate setup into a structured, human-readable trade idea object:

- It only structures scored candidates. It does not score raw market data, send alerts, place trades, route orders, call private exchange APIs, or perform live execution.
- It creates a trade idea only when hard quality gates pass: opportunity decision is not `reject`, opportunity score is at least 80, risk is approved, invalidation is present, stop loss is present, entry zone is present, at least one take profit target exists, and best risk/reward is at least 2.0.
- Passing ideas are `conditional` by default. They become `active` only when the input explicitly sets `entry_triggered=True`.
- Rejected candidates return `status: rejected` with structured quality-gate violations.
- Missing technical or derivatives summaries are marked as `N/A` in the deterministic reason text and missing-data list.
- Unverified data supplied by earlier phases is preserved.
- Every result includes a risk warning. If leverage is supplied, additional liquidation-risk language is included, with high, extreme, and dangerous leverage warnings when thresholds are exceeded.

Example output:

```json
{
  "symbol": "BTCUSDT",
  "exchange": "Binance",
  "market_type": "perpetual",
  "direction": "long",
  "timeframe": "1h",
  "setup_type": "liquidity_sweep_reclaim",
  "status": "conditional",
  "entry_zone": {
    "label": "entry_zone",
    "price": "N/A",
    "low": "100.00000000",
    "high": "102.00000000"
  },
  "stop_loss": {
    "label": "stop_loss",
    "price": "95.00000000",
    "low": "N/A",
    "high": "N/A"
  },
  "invalidation": "Price closes below the reclaimed range low.",
  "take_profits": [
    {
      "target_number": 1,
      "price": "112.00000000"
    }
  ],
  "best_rr": "3.50000000",
  "confidence_score": "88.00000000",
  "grade": "A",
  "reason_for_trade": "Technical context: Bullish sweep and reclaim at support. Derivatives context: Open interest confirms participation without crowding.",
  "confirmed_facts": ["Range low reclaimed"],
  "missing_data": [],
  "unverified_data": [],
  "cancel_condition": "Cancel if price accepts below the entry zone before trigger.",
  "risk_warning": "This is not financial advice. Position size must be based on stop-loss risk, not desired profit.",
  "quality_gate_result": {
    "passed": true,
    "violations": []
  }
}
```

## Phase 8 Alert Agent

Phase 8 adds `AlertAgent` under `app/agents` and alert helpers under `app/alerts` for turning structured trade ideas into readable alert messages:

- Alerts are dry-run by default. If `dry_run` is omitted or `True`, no Telegram request is made and the formatted message is returned with status `dry_run`.
- Live Telegram delivery happens only when `dry_run=False`, `channel="telegram"`, `telegram_bot_token` is provided, and `telegram_chat_id` is provided.
- Telegram delivery uses async `httpx`, sends plain text without fragile Markdown formatting, splits long messages safely, and handles timeouts, non-200 responses, rate limits, and malformed responses.
- Tests use `httpx.MockTransport` and do not call the live Telegram API.
- Deduplication keys can be passed through and marked in the result, but persistent deduplication is intentionally not implemented yet.
- The alert agent does not call exchanges, use private exchange APIs, place orders, route orders, or execute trades.
- Missing data remains `N/A`, unreliable data remains `Unverified`, and every formatted alert includes a risk warning.

Example dry-run usage:

```python
from app.agents.alert_agent import AlertAgent

result = await AlertAgent().send({"trade_idea": trade_idea})
assert result.status == "dry_run"
print(result.formatted_message)
```

Example alert message:

```text
🟢 Trade Setup Alert — BTCUSDT

Direction: long
Exchange: Binance
Market type: perpetual
Timeframe: 1h
Setup type: liquidity_sweep_reclaim
Status: conditional
Entry zone: 100.00000000 - 102.00000000
Stop loss: 95.00000000
Invalidation: Price closes below the reclaimed range low.
Take profits: TP1: 112.00000000; TP2: 120.00000000
Best R:R: 3.50000000
Confidence score: 88.00000000
Grade: A
Reason for trade: Technical context: Bullish sweep and reclaim at support. Derivatives context: Open interest confirms participation without crowding.
Confirmed facts: Range low reclaimed
Missing data: N/A
Unverified data: funding: Unverified
Cancel condition: Cancel if price accepts below the entry zone before trigger.
Risk warning: This is not financial advice. Position size must be based on stop-loss risk, not desired profit.

Candle Craft | Signal. Structure. Execution.
```

## Phase 9 Journal Agent

Phase 9 adds `JournalAgent` under `app/agents` for tracking generated trade ideas and alerts after they are created:

- Journal entries are structured in memory only. Database persistence, deduplication storage, and reporting dashboards are intentionally deferred to a later phase.
- The agent records setup context, risk context, optional chart/notes context, current journal status, and later performance outcome as `result_r`.
- It does not call exchanges, use private API access, place orders, route orders, or execute trades.
- Missing optional list data is marked as `N/A`; supplied unreliable data is preserved as `Unverified`.
- Updates can change only status, `result_r`, notes, emotional notes, and screenshot URL. Original setup fields such as symbol, direction, entry, stop loss, targets, invalidation, reason, and risk warning are preserved.
- Performance summaries calculate totals, selected status counts, win/loss counts, win rate, average R, best R, worst R, and best/worst setup type from supplied in-memory entries.

Supported statuses:

- `watching`
- `triggered`
- `invalidated`
- `tp1_hit`
- `tp2_hit`
- `tp3_hit`
- `stopped`
- `closed`
- `cancelled`

Win/loss logic:

- `result_r > 0` is a win.
- `result_r < 0` is a loss.
- `result_r == 0` or missing is neutral/unresolved and is excluded from win-rate calculation.

Example journal entry:

```python
from decimal import Decimal

from app.agents.journal_agent import create_journal_entry

entry = create_journal_entry(
    {
        "trade_idea_id": "idea-1",
        "alert_id": "alert-1",
        "symbol": "BTCUSDT",
        "exchange": "Binance",
        "direction": "long",
        "timeframe": "1h",
        "setup_type": "liquidity_sweep_reclaim",
        "status": "watching",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("112"), Decimal("120")),
        "invalidation": "Price closes below the reclaimed range low.",
        "best_rr": Decimal("3.5"),
        "confidence_score": Decimal("88"),
        "grade": "A",
        "reason_for_trade": "Technical context confirmed with derivatives participation.",
        "confirmed_facts": ("Range low reclaimed",),
        "missing_data": ("funding: N/A",),
        "unverified_data": ("open_interest: Unverified",),
        "risk_warning": "This is not financial advice. Size from stop-loss risk only.",
        "notes": "Watch the reclaim retest.",
    }
)
```

## Phase 10 Scanner Runner

Phase 10 adds the first scanner pipeline under `app/pipeline`:

- `ScannerRunner` connects the existing read-only modules into one safe flow: public market data, technical structure, derivatives/orderflow, risk validation, opportunity scoring, trade idea creation, dry-run alert formatting, and journal entry creation.
- It does not place trades, route orders, use private exchange API access, withdraw funds, transfer funds, or require exchange API keys.
- Alerts are dry-run by default. The runner formats the alert through the alert agent but does not send live Telegram messages unless a caller explicitly disables dry-run behavior and supplies live alert settings.
- Trade ideas are created only after technical context, derivatives checks, risk-manager gates, opportunity-scoring gates, and the configured minimum scanner score pass.
- Missing data is preserved as `N/A`; unreliable data is preserved as `Unverified`.
- If a symbol fails, the failure is recorded on that symbol and the scanner continues with the next symbol.
- Weak setups are rejected. If there is no sweep, BOS, or CHoCH context, the symbol returns `scanned_no_setup`.

Manual dry-run scan:

```powershell
python scripts/run_scan.py
```

The script scans `BTCUSDT`, `ETHUSDT`, and `SOLUSDT` on Binance Futures by default using public endpoints only. It prints a scanner summary and keeps alerts dry-run.

## Phase 11 Liquidity-Grab Pullback Engine

Phase 11 adds `LiquidityGrabEngine` under `app/strategies` for deterministic setup detection and formatting:

- It analyzes supplied candles and context only. It does not call live APIs, use private exchange access, place orders, route orders, calculate exchange liquidation, withdraw funds, or transfer funds.
- The required model is strict: confirmed liquidity sweep, BOS/CHoCH by candle close, limit pullback into a refined OB/FVG, fib alignment, RR validation, and Trust Meter gating.
- Hard requirements reject the setup. The engine prefers no setup over weak setup and does not loosen gates to create more alerts.
- Missing optional derivatives/orderflow/context data is marked `N/A`; supplied unreliable context containing `Unverified` is preserved as `Unverified`.

Supported modes:

- `challenge`: strictest mode. Uses 2D HTF structure, 12H/4H bias, and 15m/5m execution. Requires Trust Meter >= 85, RR >= 3.0, fixed 5% risk text, limit pullback entries only, no meme/illiquid token classification, and no active BTC/event guard when provided. Invalid Challenge output exposes the exact message `No valid challenge setup.`
- `swing`: uses 2D/12H structure with 4H or 1H execution where available, with 15m/5m fallback, and applies the base RR >= 2.5 gate. Invalid Swing output exposes the exact message `No valid swing setup.`
- `scalp`: uses 12H/4H bias with 15m/5m execution and requires the pullback entry to remain valid within the short LTF window. Invalid Scalp output exposes the exact message `No valid scalp setup.`

Trust Meter scoring totals 12 points:

- Sweep magnitude: 0 to 2
- Clean BOS/CHoCH: 0 to 2
- OB/FVG quality: 0 to 2
- Fib alignment: 0 to 1
- Volume/Delta confirmation: 0 to 2
- HTF bias alignment: 0 to 2
- BTC/BTC.D context supportive: 0 to 1

Trust Meter percent is `min(100, 10 * score + 20)`. Grades are `A` at 85% or higher, `B` from 75% to 84%, and `No trade` below 75%.

Shortened formatted example:

```text
🟢 Challenge Setup
No valid challenge setup.

🔵 Swing Setup
1) HTF Structure (2D)
• Current price: [112].
• Trend: [bullish].
• Key levels: Support [N/A], Resistance [120].

3) Trade Map
• Bias: [long].
• Sweep Zone: [85 -> 90].
• Entry: [97 - 99].
• Stop: [83.275].
• TPs: [TP1 112], [TP2 142.175], [TP3 opt N/A].
• RR: [3].
• Trust Meter: [A + 100%].
👉 SOLUSDT = Swing A.

⚔️ Candle Craft | Signal. Structure. Execution.
```

## Phase 11.1 Scanner + Strategy Diagnostics

Phase 11.1 adds readable diagnostics to the Scanner Runner and the Liquidity-Grab Pullback Engine:

- Scanner symbol results now include per-symbol diagnostics such as candles fetched, latest close, technical and derivatives scores, trend/range context, swing levels, sweep/BOS/CHoCH flags, funding/OI context, rejection stage, rejection reasons, missing data, and unverified data.
- `scripts/run_scan.py --verbose` prints a readable diagnostic block for each scanned symbol so rejected or no-setup symbols are explainable.
- `scripts/run_scan.py --output-json scan_output.json` writes the full `ScannerRunResult`, including diagnostics, without API keys or secrets.
- Liquidity-grab setup results now explain sweep, BOS/CHoCH, OB/FVG, fib alignment, momentum, RR, Trust Meter, the first failed gate, and the final decision.
- Diagnostics help understand rejected setups. They do not loosen quality gates, bypass hard rejections, force trade ideas, place orders, or use private exchange API access.
- Missing data remains `N/A`; unreliable supplied context remains `Unverified`.

Verbose scanner run:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --interval 15m --candle-limit 250 --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --verbose
```

Save scanner JSON output:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --output-json scan_output.json
```

## Phase 12 Scanner Strategy Integration

Phase 12 connects the Phase 11 Liquidity-Grab Pullback Engine into the Phase 10 Scanner Runner:

- `ScannerRunConfig` now supports `strategy_name`, `strategy_modes`, `enable_strategy_output`, `include_formatted_strategy_output`, `aggressive_toggle`, `htf_timeframe`, `bias_timeframe`, `execution_timeframe`, and `confirmation_timeframe`.
- The default strategy is `liquidity_grab_pullback` with `challenge`, `swing`, and `scalp` modes.
- For each symbol, the scanner collects Phase 12.1 multi-timeframe context: synthetic 2D from 1D candles, direct 12H bias candles, direct 15m execution candles, and direct 5m confirmation candles. Existing 4H and 1H context remains optional when available.
- Strategy results, formatted Candle Craft output, diagnostics, valid/rejected modes, missing data, and unverified data are included in the scanner result and JSON export.
- A trade idea is created only when the strategy returns at least one valid A/B setup and the existing technical, derivatives, risk, scoring, and trade-idea gates also pass.
- If no valid Liquidity-Grab Pullback setup exists, the symbol returns `scanned_no_setup`, `rejection_stage = strategy`, and `No valid Liquidity-Grab Pullback setup.`
- Rejected strategy setups are diagnostics only. They are not signals, do not create trade ideas, do not create alerts, and do not create journal entries.
- Telegram remains dry-run by default. The scanner does not live-send Telegram alerts unless `dry_run_alerts=False` is explicitly provided by a caller.
- No trades are placed, no orders are routed, no private exchange API access is used, and no withdrawals or transfers exist.

Run the scanner with strategy output:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --interval 15m --candle-limit 250 --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --verbose --show-strategy-output
```

CLI behavior:

- `--diagnostics-level summary` prints one compact line per scanned symbol.
- `--diagnostics-level normal` is the default and prints a readable per-symbol block.
- `--diagnostics-level full` prints the detailed scanner diagnostics and strategy diagnostics.
- `--verbose` maps to `--diagnostics-level full` unless `--diagnostics-level` is explicitly provided.
- `--show-strategy-output` prints the formatted Challenge, Swing, and Scalp Candle Craft sections for each symbol.
- `--telegram-format` changes `--show-strategy-output` to print Telegram-ready Candle Craft messages. It formats text only and does not send Telegram messages.
- `--htf-timeframe`, `--bias-timeframe`, `--execution-timeframe`, and `--confirmation-timeframe` default to `2d`, `12h`, `15m`, and `5m`.
- `--output-json scan_output.json` writes the full scanner result, including strategy results, formatted output, diagnostics, `missing_data`, and `unverified_data`, without secrets or API keys.

Missing data policy:

- Missing data is always marked `N/A`; unreliable supplied data is preserved as `Unverified`.
- Missing synthetic 2D or direct 12H context is marked `N/A`.
- Missing 5m confirmation candles are marked `N/A`, reject the setup, and do not crash the scan if 15m execution candles are available.
- Missing CVD and liquidation context remain `N/A` unless supplied by a caller.
- Malformed required candles reject the affected symbol with a clear failure reason, and the scanner continues to the next symbol.

## Phase 12.1 MTF Timeframe Model

Phase 12.1 updates the scanner and Liquidity-Grab Pullback integration to match Candle Craft multi-timeframe logic:

- `2D` is high-timeframe structure: trend, major support/resistance, and macro sweep zones.
- `12H` is active directional bias and liquidity context.
- `15m` is primary execution: sweep, OB/FVG pullback, fib alignment, RR, volume/delta when available, and Trust Meter gates.
- `5m` is mandatory lower-timeframe BOS/CHoCH confirmation after the sweep.
- `4H` and `1H` remain extra context only where the scanner already supports them.

Binance Futures does not support `2d` on `/klines`, so the scanner does not request Binance interval `2d`. It fetches `1d` candles, merges every two complete daily candles into one synthetic 2D candle, and marks `htf_2d_context_source = synthetic_from_1d`. If a complete synthetic 2D series cannot be created, `candles_2d: N/A` is preserved.

The strategy never creates a setup from 2D or 12H alone. Higher timeframes provide context only; execution confirmation still requires the strict setup gates: confirmed 15m sweep, mandatory 5m BOS/CHoCH, 15m OB/FVG pullback, fib alignment, volume/delta confirmation when available, RR gate, and Trust Meter gate. Weak setups remain rejected.

Verbose and JSON diagnostics include:

- `htf_timeframe`, `bias_timeframe`, `execution_timeframe`, `confirmation_timeframe`
- `htf_2d_context_source`
- `candles_2d_count`, `candles_12h_count`, `candles_15m_count`, `candles_5m_count`
- `htf_2d_trend`, `mtf_12h_trend`
- `ltf_confirmation_timeframe`, `ltf_confirmation_status`
- `execution_sweep_status`, `confirmation_structure_shift_status`, `confirmation_bos_choch_reason`
- `first_failed_gate`

This remains dry-run analysis only. The scanner and strategy do not place orders, do not use private exchange API access, and do not add withdrawal or transfer functionality.

## Phase 12.2 Confirmation Timeframe Fix

Phase 12.2 tightens the multi-timeframe confirmation path and makes scanner output easier to read:

- `2D` is HTF structure and major sweep-zone context.
- `12H` is active directional bias.
- `15m` is execution: sweep, OB/FVG, fib, entry, stop, RR, and execution-side trade mapping.
- `5m` is mandatory BOS/CHoCH confirmation after the 15m sweep.
- No setup is created from 2D or 12H context alone.
- If 5m BOS/CHoCH confirmation fails, the setup is rejected with `first_failed_gate = missing_confirmation_structure_shift`.
- If 5m candles are missing, confirmation timeframe is marked `N/A`, the setup is rejected, and the reason is `5m confirmation candles missing.`

Scanner JSON diagnostics now include the configured and resolved timeframe context:

- `htf_timeframe`, `bias_timeframe`, `execution_timeframe`, `confirmation_timeframe`
- `htf_2d_context_source`
- `candles_2d_count`, `candles_12h_count`, `candles_15m_count`, `candles_5m_count`
- `execution_sweep_status`
- `confirmation_structure_shift_status`
- `confirmation_bos_choch_reason`
- `first_failed_gate`

Diagnostics levels:

- `summary`: one compact line per symbol.
- `normal`: default readable block per symbol.
- `full`: all detailed diagnostics retained for debugging.

Rejected formatted strategy output stays concise:

- `Challenge: No valid challenge setup.`
- `Swing: No valid swing setup.`
- `Scalp: No valid scalp setup.`

Run Phase 12.2 scanner output:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output
```

## Phase 13 Volume Profile / POC Engine

Phase 13 adds a deterministic candle-based Volume Profile / POC engine under `app/analytics/volume_profile.py` and threads its output into scanner diagnostics and Liquidity-Grab Pullback context.

- The source is always marked as `volume_profile_source = "estimated_from_candles"`.
- This is an OHLCV candle approximation, not tick-level true volume profile.
- Candle volume is allocated across price buckets using each candle's high/low range.
- Missing volume keeps POC, VAH, VAL, HVN, and LVN context as `N/A`; the scanner does not invent profile levels.
- POC, VAH, VAL, high-volume nodes, and low-volume nodes are diagnostics and confluence only.
- POC alone never creates a setup, never loosens strategy gates, and never bypasses sweep, 5m BOS/CHoCH, OB/FVG, fib, RR, or Trust Meter requirements.
- Rejected setups stay rejected even when POC is available.
- Scanner JSON includes the volume profile result and source without secrets or API keys.
- Alert behavior remains dry-run by default.

Scanner summary mode shows `POC` only when available. Normal mode shows `POC`, `VAH`, `VAL`, and the source. Full mode includes volume profile diagnostics, HVN/LVN context, warnings, and the optional 12H profile when enough 12H candles are available.

Run Phase 13 scanner output:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output
```

## Phase 14 Pullback Zone Engine

Phase 14 adds `app/analytics/pullback_zones.py` for the Liquidity-Grab Pullback strategy. The engine runs only after the 15m sweep and 5m BOS/CHoCH confirmation have passed.

- The engine identifies the displacement impulse from the sweep wick to the BOS/CHoCH impulse extreme.
- Bullish and bearish FVGs are detected from the displacement candles and marked with high, low, midpoint, creation index, and freshness.
- Bullish and bearish order blocks are detected as the last opposite-color candle before displacement/BOS and include body, wick, midpoint, creation index, and freshness.
- A selected pullback zone must overlap the 0.382 to 0.618 fib retracement range. Aggressive mode can allow drift to 0.65.
- Body-close acceptance beyond 0.786 or persistent acceptance beyond the invalidation zone is rejected. Wick-only breaches are classified by the Phase 41 wick/close layer instead of being treated as automatic `pullback_too_deep`.
- Stops use the sweep wick plus a 0.10 ATR(15m) buffer, or the farther OB structure edge when that is more conservative. If ATR is unavailable, the structure edge is used and the ATR buffer remains `N/A`.
- TP1 uses the nearest opposing liquidity/range level when available, otherwise fib 1.272. TP2 uses fib 1.618 and TP3 uses fib 2.0.
- RR to TP2 must be at least 2.5 for swing/scalp and at least 3.0 for challenge mode. Failed RR rejects with `rr_too_low`.

No setup is created unless the pullback zone and RR are valid. Missing or uncertain OB/FVG/fib fields remain `N/A`, and rejected setups stay rejected. POC, VAL, and VAH remain confluence only; they can annotate a selected zone but never create a setup by themselves.

Scanner JSON and full diagnostics include pullback fields such as `pullback_zone_status`, `selected_zone_type`, `ob_zone`, `fvg_zone`, `fib_alignment_status`, `fib_382`, `fib_618`, `fib_65`, `fib_786`, `entry_low`, `entry_high`, `stop`, `tp1`, `tp2`, `tp3`, `rr_to_tp2`, and `pullback_failure_reason`. Normal CLI output includes:

```text
Pullback:
Status: valid/failed
OB/FVG: selected zone or N/A
Fib: status or N/A
RR: value or N/A
Reject: failed gate or N/A
Reason: rejection reason or N/A
```

Run Phase 14 scanner output:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output
```

## Phase 15 Derivatives Enrichment Layer

Phase 15 adds `app/analytics/derivatives_enrichment.py` and threads `DerivativesEnrichmentResult` into scanner results, CLI diagnostics, JSON output, and the Liquidity-Grab Pullback strategy context.

- The layer uses public futures market data only. Binance Futures and Bybit Linear clients can collect current funding, funding history, open interest, open interest history, and public long/short ratio where the exchange endpoint is available.
- It does not use API keys, private exchange access, account endpoints, order endpoints, withdrawals, transfers, or live execution.
- Funding is classified as `normal`, `elevated_positive`, `extreme_positive`, `elevated_negative`, `extreme_negative`, or `N/A`.
- Open interest change is calculated from current and previous OI when both are available. Missing current or previous OI keeps `open_interest_change_pct = N/A`.
- Price/OI relationship is deterministic: price up + OI up, price up + OI down, price down + OI up, price down + OI down, and neutral/flat context each map to a fixed classification.
- Crowding risk combines funding, public long/short ratio, and OI expansion when available.
- Squeeze risk estimates long-squeeze, short-squeeze, balanced, or `N/A` context from funding extremes, OI expansion, price direction, and long/short imbalance.
- `derivatives_score` measures how useful and complete the derivatives context is. It is not a profitability score.
- Missing derivatives fields are marked `N/A`; malformed or endpoint-inconsistent fields are preserved as `Unverified` with warnings.
- Public endpoint failures do not fail the scan. The scanner keeps going and records missing/warning diagnostics.

Derivatives are confluence/filter only. They never create a trade setup by themselves and never override hard technical gates:

- No sweep means no trade.
- No 5m BOS/CHoCH means no trade.
- No OB/FVG/fib alignment means no trade.
- RR below the strategy threshold means no trade.
- Trust Meter requirements still apply.

The Liquidity-Grab Pullback strategy can reject only severe derivatives conflict after technical gates have passed, such as extreme funding directly against the trade, aggressive OI expansion against the trade, high crowding risk against the trade, and no clear absorption. Missing derivatives data does not reject a setup by itself.

Scanner summary output includes compact derivatives context when available:

```text
BTCUSDT | No Setup | 2D: bearish | 12H: bearish | POC: 79704 | Funding: negative/normal | OI: rising | 15m sweep: passed | 5m BOS/CHoCH: passed | Pullback: failed | Reject: body_acceptance_failure
```

Normal CLI output includes a `Derivatives` block with funding, OI, price/OI, crowding, squeeze, and context score. Full diagnostics and JSON output include the nested `derivatives_enrichment` object.

Run Phase 15 scanner output:

```powershell
python scripts/run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output
```

## Phase 15.1 Scanner Output Cleanup

Phase 15.1 keeps scanner behavior and JSON field names backward compatible while cleaning up CLI wording:

- Runtime scanner output uses the phase-neutral `Candle Craft Scanner Runner` label.
- The derivatives score is displayed as context/data clarity, not trade profitability. Internal JSON keeps the existing `derivatives_score` field name.

## Phase 15.2 Confirmation Pullback Integration

Phase 15.2 fixes the multi-timeframe confirmation-to-pullback handoff:

- The Liquidity-Grab Pullback strategy keeps the 15m execution sweep context while using the 5m confirmation BOS/CHoCH displacement for pullback-zone calculation.
- Pullback diagnostics now expose the calculation timeframe, sweep index, BOS/CHoCH index, and displacement start/end indices.
- Rejection reasons are more specific, including `missing_confirmation_candles`, `no_displacement_candle`, `no_ob_or_fvg_zone`, `wick_sweep_reclaim`, `body_acceptance_failure`, `structural_breakdown`, and `rr_below_minimum`.
- Normal CLI output prints one failed gate, one reason, and one action instead of duplicating pullback rejection lines.

## Phase 16 Telegram Formatter

Phase 16 adds `app/formatters/telegram_formatter.py` for Telegram-ready Liquidity-Grab Pullback scanner output:

- Valid setups are formatted as concise Candle Craft trade-map messages with HTF structure, orderflow/derivatives context, entry, stop, targets, RR, Trust Meter, invalidation, and risk warning.
- Rejected setups are formatted as no-valid-setup messages with 2D/12H context, 15m sweep status, 5m BOS/CHoCH status, pullback status, failed gate, clean reason, and no-trade action.
- Missing values remain `N/A`; unreliable values remain `Unverified` when present in scanner output.
- Compact and full diagnostic formatter modes are available. `--diagnostics-level full` keeps diagnostic detail available when printing Telegram-ready strategy output.
- The formatter is output-only. It does not call Telegram, create alerts, place orders, use private exchange endpoints, withdrawals, transfers, or change strategy gates.

Run Phase 16 Telegram-ready scanner output:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level summary --show-strategy-output --telegram-format
```

## Phase 17 Premium Scanner Display

Phase 17 adds `app/formatters/scanner_display.py` for clean Candle Craft scanner cards, dashboard counts, near-miss diagnostics, and extended JSON display metadata:

- Console output starts with a scanner dashboard and classifies each symbol with display metadata. Phase 18 extends this with ranked `valid`, `near_miss`, `no_setup`, and `data_issue` buckets.
- `--display compact|normal|full` controls output shape. `compact` prints the dashboard plus one result line per symbol, `normal` prints dashboard plus premium cards, and `full` adds detailed diagnostics after each card.
- Near misses are diagnostics only: they require the 15m sweep and 5m BOS/CHoCH confirmation to pass while a later pullback/RR/quality gate fails. They do not create signals, alerts, journal entries, or trade ideas.
- `--telegram-format` now prints shorter Telegram-ready diagnosis blocks with bias, passed checks, failed checks, reason, action, and the Candle Craft footer.
- `--output-json` preserves existing scanner fields and adds `display_status`, `display_status_label`, `setup_progress_total`, `setup_progress_passed`, `passed_checks`, `failed_checks`, `short_reason`, `action_label`, and Phase 22 `near_miss_intelligence` when applicable.
- This phase is formatting/output UX only. It does not change strategy gates, sweep/BOS/CHoCH/OB/FVG/RR rules, exchange access, alert sending, order execution, withdrawals, or transfers.

Run Phase 17 premium scanner output:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level summary --show-strategy-output --telegram-format --display normal
```

## Phase 18 Scanner Result Ranking

Phase 18 adds an output-only ranking and filtering layer for scanner results so the CLI shows the most actionable or near-actionable symbols first without changing strategy behavior.

Buckets:

- `valid` / `🟢 VALID SETUP`: a trade idea was created after scanner gates passed.
- `near_miss` / `🟡 NEAR MISS`: the 15m sweep and 5m BOS/CHoCH confirmation passed, then a later pullback, OB/FVG, fib, RR, or final confluence gate failed.
- `no_setup` / `⚪ REJECTED`: the setup failed before sweep or 5m structure confirmation, or otherwise never became near-actionable.
- `data_issue` / `🔴 DATA ISSUE`: required scanner data was missing, unavailable, malformed, or otherwise insufficient.

Default display behavior:

- Results are ranked by bucket first: valid setup, near miss, no setup, then data issue. Phase 23 overrides this with setup-quality priority when `setup_quality` is evaluated.
- Within a bucket, ranking prioritizes created trade ideas, higher Trust Meter or setup score, better RR, stronger context score, derivatives support, fewer failed gates, and later-stage failures.
- Normal CLI output shows valid setups first, near misses second, and data issues after that. Weak rejected/no-setup symbols are counted in the dashboard but hidden unless `--show-no-setups` or `--bucket-filter no_setup` is used.
- Normal display shows at most 10 result cards by default. Use `--max-display-results N` to change the display limit.
- `--display full` keeps detailed diagnostics while sorting by the Phase 18 rank first.
- JSON output keeps all scanner results and adds `display_rank`, `display_bucket`, `display_priority_score`, `display_reason`, `hidden_by_default`, `failed_stage`, and `action_label`. Phase 23 also includes the nested `setup_quality` object on each scanner result.

Run ranked scanner output:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --display normal --max-display-results 20
```

Useful filters:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --bucket-filter valid,near_miss --display compact
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --show-no-setups --display full
```

Safety note: Phase 18 only prioritizes display output. It does not create trades, loosen or modify Liquidity-Grab Pullback strategy gates, alter sweep/BOS/CHoCH/OB/FVG/fib/RR/risk/derivatives logic, place orders, use private exchange endpoints, or add withdrawals/transfers.

## Phase 19 Watchlist Presets

Phase 19 adds static scanner watchlist presets under `app/watchlists/presets.py` and CLI resolution in `scripts/run_scan.py` so larger batches can be scanned without typing every symbol manually.

- Presets are curated static symbol lists, not current top market-cap rankings.
- Available presets: `majors`, `large_caps`, `l1_l2`, `sol_ecosystem`, `ai`, `rwa`, `defi`, and `meme_high_liquidity`.
- `--symbols` keeps the existing manual symbol behavior.
- `--include-symbols` appends symbols, `--exclude-symbols` removes symbols, duplicates are removed while preserving order, and `--max-symbols` trims after include/exclude processing.
- `--preset-file` accepts JSON shaped like `{"name": "custom_name", "symbols": ["BTCUSDT", "ETHUSDT"]}`.
- Scanner output prints the watchlist source and symbol count before scanning, then keeps the Phase 18 ranked display behavior.

List presets:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --list-presets
```

Scan majors:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset majors --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal
```

Scan large caps with a symbol cap:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset large_caps --max-symbols 10 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal
```

Scan a preset with exclusions:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset large_caps --exclude-symbols DOGEUSDT XRPUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal
```

Scan a custom preset file:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset-file .\custom_watchlist.json --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal
```

Safety note: Phase 19 only resolves scanner inputs. It does not create trades, loosen or modify Liquidity-Grab Pullback strategy gates, alter sweep/BOS/CHoCH/OB/FVG/fib/RR/Trust Meter/risk rules, place orders, use private exchange endpoints, send live Telegram messages, or add withdrawals/transfers.

## Phase 20 Batch-Scan Reliability

Phase 20 improves larger watchlist scans without changing strategy gates, result ranking, or watchlist preset behavior.

- Public market-data caching is enabled in memory by default for the current scan run.
- Optional file caching is available only with `--cache-file PATH`.
- `--no-cache` disables all cache reads and writes.
- Cached data is limited to public GET market-data responses: candles/klines, ticker, funding, open interest, open interest history, and public long/short ratio where supported.
- Cache keys include exchange, symbol, endpoint/data type, interval/timeframe, limit, and relevant params. Failures are never cached.
- Default TTLs are candles 60s, ticker 15s, funding 60s, open interest 30s, and long/short ratio 60s. `--cache-ttl-seconds N` overrides the TTL for a run.
- Retry/backoff events from public HTTP clients are exposed in full diagnostics and JSON output.
- Per-symbol scan errors are isolated as `scan_error`; one failing symbol does not stop the batch.
- `--save-run PATH` writes structured JSON after each symbol finishes.
- `--resume-from PATH` loads a prior run and skips already completed non-error symbols by default. Use `--no-resume-skip` to rescan everything.

Large preset scan with concise progress:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset large_caps --max-symbols 10 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal --progress
```

Save and resume a large scan:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset large_caps --max-symbols 10 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal --progress --save-run scan_runs\latest_scan.json

.\.venv\Scripts\python.exe scripts\run_scan.py --preset large_caps --max-symbols 10 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal --progress --resume-from scan_runs\latest_scan.json
```

Disable caching completely:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset majors --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --show-strategy-output --rank-results --display normal --progress --no-cache
```

Safety note: Phase 20 is reliability and output persistence only. It does not create trades, change Phase 18 ranking, change Phase 19 preset resolution, loosen sweep/BOS/CHoCH/pullback/fib/RR/Trust Meter/risk rules, place orders, use private exchange endpoints, send live Telegram messages, or add withdrawals/transfers.

## Phase 21 Symbol Universe Layer

Phase 21 adds public-data-only scanner universes so larger Binance USD-M futures scans can be resolved without manually typing every symbol.

- `--universe manual` preserves existing `--symbols`, `--preset`, and `--preset-file` behavior.
- `--universe binance_usdt_perp_top_volume` resolves the top Binance USD-M USDT symbols by public 24h ticker `quoteVolume`.
- `--universe binance_usdt_perp_top_tradable` is labeled as "Top Binance USDT perpetuals by quote volume/tradability". It uses public Binance USD-M 24h ticker quote volume, then filters obvious bad scan targets such as stablecoin/stable pairs, leveraged tokens, missing quote volume, non-USDT symbols, and symbols below `--min-quote-volume`. This is not a market-cap ranking.
- `--universe binance_usdt_perp_top_market_cap` resolves a true public market-cap ranking from a no-key public source, then intersects ranked base assets with tradable Binance USDT perpetual symbols. If the market-cap source is unavailable, the CLI returns a clean `universe_error` message and does not invent rankings.
- `--universe-size N` controls how many symbols are requested after each universe's public ranking/filtering step.
- `--min-quote-volume N` is optional and defaults to `0`.
- Scanner headers and saved JSON include the selected universe mode, clear display label, public source, requested size, resolved symbols, excluded symbols, generated time, minimum quote volume, and top resolved symbols by quote volume or market-cap rank where available.
- The dashboard aggregates optional public endpoint failures as `Data warnings: X optional endpoint warnings.` Retry/backoff details stay out of normal output and remain available in `--diagnostics-level full` and JSON diagnostics.
- `--display compact|normal|full` controls line, card, and detailed-card output. Normal display shows the dashboard plus visible cards, sorted as valid setups first and near misses second, with a default limit of 10 cards unless `--max-display-results` is set.
- `--diagnostics-level summary|normal|full` controls how much diagnostic detail is shown by supporting formatters. Full diagnostics include retry attempts; summary and normal keep retry noise hidden.

Example A - manual scan:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --strategy liquidity_grab_pullback
```

Example B - top 50 Binance USDT perpetuals by quote volume/tradability:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --universe binance_usdt_perp_top_tradable --universe-size 50 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --display normal --rank-results --max-display-results 10 --save-run
```

Example C - top 50 Binance USDT perpetuals by public market cap:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --universe binance_usdt_perp_top_market_cap --universe-size 50 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --diagnostics-level normal --display normal --rank-results --max-display-results 10 --save-run
```

Safety note: Phase 21 resolves scanner inputs from public market data only. It does not create trades, weaken strategy gates, use private exchange API keys, call private/account/order endpoints, place orders, send live Telegram messages, withdraw funds, or transfer funds.

## Phase 22 Near-Miss Intelligence

Phase 22 adds `app/analytics/near_miss_intelligence.py`, an output-only explanation layer for symbols that are close but not valid. It reads existing scanner diagnostics and failed gates, then adds a `near_miss_intelligence` object to scanner results and JSON output when a watchlist/no-trade plan is useful.

Each near-miss intelligence object includes:

- `primary_failed_gate`
- `short_reason`
- `watchlist_status`
- `next_required_conditions`
- `activation_hint`
- `invalidation_hint`
- `quality_note`
- `action_label`

Behavior examples:

- `rr_below_minimum`: status is `Watchlist only`; the plan explains that RR must improve through a better pullback entry, wider TP2 distance, or cleaner opposing liquidity target before the setup can become valid.
- `pullback_too_deep`, `body_acceptance_failure`, or `structural_breakdown`: status is `Rejected`; the plan explains that body acceptance or structural breakdown beyond 0.786 requires fresh structure.
- `no_ob_or_fvg_zone`: status is `Watchlist only` only when the sweep and BOS/CHoCH passed; otherwise it stays `Rejected`.
- `missing_confirmation_structure_shift`: status is `Wait for confirmation`; the plan waits for a 5m BOS/CHoCH close before any pullback, RR, risk, or trade-idea logic can matter.

Normal near-miss cards now include a short action plan:

```text
BTCUSDT — NEAR MISS
Status: Watchlist only
Failed gate: rr_below_minimum
Reason: RR to TP2 is below the required minimum.

Needs next:
1. Better pullback entry must improve entry-to-stop distance.
2. TP2 distance must widen without inventing a target.
3. A cleaner opposing liquidity target must be visible before activation.

Activation hint: RR must improve to the required minimum before this setup can become valid.
Invalidation hint: Invalidated if the sweep/BOS/CHoCH context fails, expires, or price invalidates the strategy structure.
Action: Watchlist only
```

`--display full` includes the near-miss diagnostics alongside the existing scanner diagnostics. `--show-near-miss-plan` can print the same plan block when compact display is selected.

Telegram formatting remains text-only and no-trade-first. For watchlist or confirmation-only cases it prints clean wording such as `No valid setup. No trade. Watchlist only.` or `No valid setup. No trade. Wait for confirmation.` It does not send Telegram messages by itself.

Safety note: Phase 22 is intelligence and display wording only. It does not weaken sweep, BOS/CHoCH, OB/FVG, fib, RR, Trust Meter, risk, scoring, or trade-idea gates; it does not create trade ideas from near-misses, send live Telegram alerts, place orders, use private exchange API access, withdraw funds, or transfer funds.

## Phase 23 Setup Quality / Profitability Validation

Phase 23 adds `app/analytics/setup_quality.py`, a deterministic post-strategy quality layer that decides whether a technically valid setup has enough money-making edge to be worth acting on. It runs after the existing strategy result is produced and is attached to each `ScannerSymbolResult` as `setup_quality`.

Quality states:

- `HIGH_QUALITY_TRADE`: valid scanner setup with clean sweep, clean 5m BOS/CHoCH, valid pullback, RR at or above the required threshold, strong context, non-conflicting derivatives, and acceptable execution risk.
- `VALID_BUT_LOWER_QUALITY`: valid scanner setup with one or more weaknesses such as marginal RR, weak context, mixed derivatives, late pullback, wide stop, weak volume/POC alignment, or trend conflict.
- `WATCHLIST_NEAR_MISS`: sweep and 5m BOS/CHoCH passed, but a later pullback, RR, or quality gate still needs improvement.
- `REJECTED_NO_EDGE`: setup quality does not provide enough deterministic edge.
- `DATA_ISSUE`: required market data is missing, unavailable, or unreliable enough to prevent validation.

The result includes `quality_grade`, `quality_score`, `tradeability_score`, `profitability_edge_score`, `execution_risk_score`, `strongest_factors`, `weakest_factors`, `decision_reason`, and `action_label`. `execution_risk_score` is 0-100 where lower is better; the weighted quality score uses `100 - execution_risk_score` for the execution-risk contribution.

Scoring model:

- Structure quality: 25 points from sweep, 5m BOS/CHoCH, and direction alignment.
- Pullback quality: 20 points from valid OB/FVG, fib alignment, and non-late pullback behavior.
- RR / profit potential: 20 points from RR versus the mode-specific minimum and target reach.
- Context quality: 15 points from 2D/12H alignment, Trust Meter/context score, and POC/VAH/VAL availability.
- Derivatives quality: 10 points from funding, OI, price/OI, crowding, and directional support. Missing optional derivatives reduce confidence but do not auto-reject.
- Execution risk: 10 points from risk approval, data completeness, leverage risk, stop width, and warnings.

Ranking behavior:

- Ranked output now prioritizes `HIGH_QUALITY_TRADE`, then `VALID_BUT_LOWER_QUALITY`, `WATCHLIST_NEAR_MISS`, `REJECTED_NO_EDGE`, and `DATA_ISSUE`.
- Inside each quality bucket, symbols sort by `quality_score` descending.
- If there are no valid trades, near-misses remain visible and ordered by how close they are to becoming actionable.

Display behavior:

- Compact display prints only `SYMBOL — STATE — grade — score — action`.
- Normal and full cards include Quality, Edge, Risk, Action, Strongest, Weakest, and Reason.
- Telegram formatting includes quality grade/score for valid setups. Rejected/no-trade output stays concise with `Action` and `Reason`.

Safety boundaries:

- Phase 23 does not weaken sweep, BOS/CHoCH, OB/FVG, fib, RR, risk, scoring, pullback, or Trust Meter gates.
- It does not add order execution, private exchange API access, account endpoints, withdrawals, transfers, or live Telegram sending.
- It does not invent CVD, liquidation heatmap, footprint, BTC.D, event calendar, or sector data. Missing unavailable inputs remain `N/A` and unreliable inputs remain `Unverified`.

Example command:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --min-score-for-idea 80 --strategy liquidity_grab_pullback --modes challenge swing scalp --htf-timeframe 2d --bias-timeframe 12h --execution-timeframe 15m --confirmation-timeframe 5m --display normal --rank-results
```

## Phase 24 Historical Replay / Backtest Validation

Phase 24 adds `app/backtesting/strategy_replay.py`, a deterministic diagnostic replay layer for the Liquidity-Grab Pullback strategy. It walks recent historical candles in chronological order, calls the existing strategy engine on candle prefixes, records valid historical setups only after the existing gates pass, and then simulates the existing limit pullback entry, stop, and TP levels using later candle high/low data.

Replay behavior:

- Uses the existing Liquidity-Grab Pullback strategy result for setup creation where possible.
- Does not weaken sweep, BOS/CHoCH, OB/FVG, fib, RR, pullback, risk, Trust Meter, or quality validation rules.
- Simulates a limit fill from the strategy result's entry price only after the pullback zone exists.
- Records `tp1_hit`, `tp2_hit`, `tp3_hit`, `stopped`, `expired`, `invalidated`, and `not_filled` outcomes.
- Calculates total setups, filled trades, win rate, TP1/TP2 rates, average R, median R, expectancy R, profit factor, max win/loss streaks, average time in trade, rejected setup count, and near-miss count.
- If both TP and stop are touched in one candle, `--same-candle-policy conservative` assumes the stop resolves first. `optimistic` can be selected explicitly.
- Missing derivatives, CVD, and liquidation heatmap context remain `N/A`. Candle-only replay never invents unavailable data.

Sample-size labels:

- `low_sample_size`: fewer than 10 filled trades.
- `medium_sample_size`: 10 to 29 filled trades.
- `usable_sample_size`: 30 or more filled trades.

Replay edge classification:

- `strong`: expectancy above `0.35R`, win rate above `45%`, and at least medium sample size.
- `mixed`: positive but not strong expectancy, or low sample size.
- `weak`: expectancy at or below `0R` with medium or usable sample size.
- `N/A`: no filled trades or no usable replay sample.

Example replay command:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py `
  --symbols BTCUSDT ETHUSDT SOLUSDT `
  --exchange binance `
  --strategy liquidity_grab_pullback `
  --replay `
  --replay-candles 1000 `
  --execution-timeframe 15m `
  --confirmation-timeframe 5m `
  --htf-timeframe 2d `
  --bias-timeframe 12h `
  --display normal
```

When `--replay` is used with `--output-json`, the output includes a top-level `replay_result` object. Replay edge is displayed as diagnostic context only; it does not replace live setup ranking.

Safety boundaries:

- Replay is diagnostic only and never places orders.
- It uses public candle data only and does not use private exchange, account, transfer, withdrawal, or order endpoints.
- Telegram remains dry-run/output-only by default.
- Existing scan artifacts are not changed unless the user explicitly uses existing output flags such as `--output-json` or `--save-run`.

## Phase 28 Portfolio Selection / Exposure Control

Phase 28 adds `app/analytics/portfolio_selection.py`, a deterministic post-scan selection layer for choosing the best opportunities when several valid setups appear at the same time. It exists to avoid treating every valid setup equally, reduce overtrading, and control correlated beta exposure and total portfolio heat.

Default rules:

- `--portfolio-select` must be enabled explicitly.
- Only valid trade candidates can be selected. Near-misses remain `WATCHLIST_ONLY`.
- Default max selected setups: `3`.
- Default max portfolio risk: `3%` total.
- Default max beta-group risk: `1.5%`.
- Correlated beta-group duplicates are rejected by default. Use `--allow-correlated-setups` only when you intentionally want multiple setups from the same beta group, still subject to the risk caps.
- Selection ranking prefers higher quality score, higher edge/expectancy, better RR, cleaner derivatives, and lower execution risk.

Beta groups:

- `BTC_MAJOR`
- `ETH_BETA`
- `SOL_BETA`
- `L1_L2`
- `MEME`
- `AI`
- `RWA`
- `DEFI`
- `UNKNOWN`

When a symbol has no reliable sector or narrative metadata, the selector marks those fields as `N/A`. Unknown beta groups are kept as `UNKNOWN` instead of inventing market data.

Example command:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --preset large_caps --max-symbols 20 --exchange binance --account-equity 1000 --risk-per-trade-pct 1 --strategy liquidity_grab_pullback --modes challenge swing scalp --display normal --rank-results --portfolio-select --max-selected-setups 3 --max-portfolio-risk-pct 3 --max-beta-group-risk-pct 1.5
```

When `--portfolio-select` and `--output-json` are used together, the output includes `portfolio_selection`, `selected_candidates`, `rejected_candidates`, `exposure_summary`, and `portfolio_warnings`.

Safety boundaries:

- Phase 28 does not weaken strategy gates or change signal generation logic.
- It does not create trades from invalid or near-miss setups.
- It does not place orders, add private exchange API access, call account endpoints, send live Telegram messages, withdraw funds, or transfer funds.

## Phase 29 Alert Watch Mode

Phase 29 adds Alert Watch Mode around the existing scanner. Near-misses are still not trades. Watch mode repeatedly re-scans a watchlist and only activates an alert when the current scan produces a real trade idea and the setup quality layer marks it `HIGH_QUALITY_TRADE` or `VALID_BUT_LOWER_QUALITY`.

Watch mode state is stored in `scan_runs/watch_state.json` and tracks each symbol's last status, failed gate, readiness score, readiness label, last seen time, whether an activation alert was already generated, activation count, and history.

Key flags:

- `--watch`: repeat the scanner on an interval.
- `--watch-interval-sec`: seconds between scans.
- `--watch-max-iterations`: stop after a fixed number of scans; omit it to keep watching until manually stopped.
- `--watch-symbols-from-latest-run`: load symbols from `scan_runs/latest_scan.json`.
- `--watch-only-near-misses`: watch only symbols previously classified as `NEAR MISS`, `HOT WATCH`, or `WATCH`.
- `--watch-output-file`: append each iteration summary as JSONL.
- `--telegram-live-alerts`: defaults to `false`. Live Telegram requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

Example near-miss watch command:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --watch --watch-symbols-from-latest-run --watch-only-near-misses --watch-interval-sec 60 --watch-output-file scan_runs/watch_iterations.jsonl
```

Live Telegram example, only after you intentionally add credentials to `.env`:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --watch --watch-symbols-from-latest-run --watch-only-near-misses --watch-interval-sec 60 --telegram-live-alerts true
```

Warning: live Telegram sends notifications only. It does not place trades. If credentials are missing, watch mode fails safely before sending anything. Dry-run is the default and prints activation messages locally.

Safety boundaries:

- Phase 29 does not weaken sweep, BOS/CHoCH, OB/FVG, fib, RR, Trust Meter, risk, scoring, setup quality, or portfolio-selection gates.
- It does not create trade ideas from near-misses, rejected setups, missing RR, missing confirmation, missing pullback zones, data issues, or low-quality rejects.
- It does not add order execution, private exchange API access, account endpoints, withdrawals, transfers, or live Telegram sending by default.

## Phase 31 Adaptive Market Regime Filter

Phase 31 adds `app/analytics/market_regime.py`, a scan-level market regime filter that helps Candle Craft avoid poor broader market conditions. It evaluates public BTCUSDT and ETHUSDT candles, candle-derived ATR/range expansion, EMA slope, and scan breadth when available. TOTAL/TOTAL2 proxies remain `N/A` unless a public proxy is actually supplied.

Regime states:

- `TREND_EXPANSION`: BTC/ETH trend and breadth align with healthy volatility.
- `CHOP`: mixed direction, weak follow-through, or frequent failed confirmations.
- `COMPRESSION`: narrow range and low ATR versus recent average.
- `PANIC_VOLATILITY`: realized range expands far above average.
- `LOW_VOL_DRIFT`: quiet conditions with weak directional drift.
- `MIXED`: conflicting BTC, ETH, breadth, or volatility context.
- `DATA_INCOMPLETE`: required BTC/ETH candles are missing.

How it affects quality/risk:

- The scanner computes one `market_regime` per scan and includes `regime_adjustments` and `regime_warnings` in JSON.
- High-risk regimes can add warnings, reduce displayed setup quality, increase effective RR/quality requirements, and apply a risk multiplier for portfolio selection.
- Challenge mode is stricter in poor regimes. For example, `PANIC_VOLATILITY` disables challenge setups, uses `risk_multiplier=0.5`, and requires larger RR.
- The regime filter never creates a setup, never makes an invalid setup valid, and never weakens existing strategy gates.

CLI flags:

- `--market-regime`: enable the filter. This is the default for scanner runs.
- `--disable-regime-filter`: disable the filter and show `Market regime filter disabled`.
- `--regime-risk-mode conservative|balanced|aggressive`: choose overlay strictness. Default is `balanced`.

Example:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --command-preset daily --symbols BTCUSDT ETHUSDT SOLUSDT --market-regime --regime-risk-mode balanced
```

Safety boundaries:

- Phase 31 uses public candle data only and scan-derived breadth. It does not invent BTC.D, USDT.D, macro, news, CVD, liquidation, TOTAL, or TOTAL2 data.
- Missing regime inputs are marked `N/A`; unreliable data must be marked `Unverified`.
- It does not add order execution, private exchange API access, account endpoints, withdrawals, transfers, or live Telegram sending.

## Phase 35 Regime Intelligence & Environment Filtering

Phase 35 moves the richer regime logic into `app/regime/`:

- `app/regime/models.py`: structured regime inputs, states, compatibility, confidence, and adjustment models.
- `app/regime/scoring.py`: weighted confidence, compatibility, penalties, boosts, and strictness scoring.
- `app/regime/classifier.py`: deterministic public-data classifier for BTC/ETH structure, volatility, breadth, sweep follow-through, HTF agreement/conflict, RR quality, setup density, and rejection clustering.

Supported regime states:

```text
TREND_EXPANSION
TREND_PULLBACK
RANGE_COMPRESSION
HIGH_VOLATILITY
LOW_VOLATILITY
CHOP
RISK_OFF
RISK_ON
MIXED
TRANSITION
```

Confidence score:

- `0-30`: hostile
- `31-50`: weak
- `51-70`: acceptable
- `71-85`: favorable
- `86-100`: exceptional

Compatibility logic:

- The scanner calculates separate `challenge`, `swing`, and `scalp` compatibility scores.
- Each mode score combines regime compatibility, volatility suitability, trend suitability, execution-quality suitability, and the scan-level confidence score.
- Weighted notes explain penalties and boosts such as HTF alignment, mixed direction, unstable volatility, broad participation, RR expansion, weak follow-through, and rejection clustering.
- A weak environment can downgrade readiness, edge, trust diagnostics, and portfolio preference. It can also turn an otherwise valid setup into a watchlist-only `rejected_by_regime` result when the selected mode is incompatible.
- Regime never creates a setup and never makes an invalid setup valid.

Strictness:

- `--regime-strictness low`: allows more borderline environments and applies smaller hostile-regime penalties.
- `--regime-strictness normal`: default balanced behavior.
- `--regime-strictness high`: applies stronger hostile-regime penalties and tighter mode acceptance.
- `--regime-risk-mode conservative|balanced|aggressive` remains supported as a backward-compatible alias for high/normal/low strictness.

CLI:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --command-preset daily --market-regime --regime-strictness high --show-regime-details
```

Stored scan history now includes regime confidence, compatibility scores, and environment notes. Phase 35 also adds research queries:

```text
regime_expectancy
regime_setup_density
regime_rejection_patterns
regime_quality_distribution
lifecycle_summary
lifecycle_transitions
lifecycle_conversion
lifecycle_funnel
lifecycle_dropoffs
lifecycle_symbol_conversion
lifecycle_state_duration
lifecycle_symbol_detail
```

Examples:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query regime_expectancy --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query regime_rejection_patterns --research-regime CHOP --database-path scan_runs/candle_craft.db
```

Safety limitations:

- Phase 35 uses already-available public data and scan-derived statistics only.
- BTC.D, USDT.D, breadth, TOTAL/TOTAL2, RR quality, setup density, and rejection clustering remain `N/A` unless actually available.
- Missing or weak context stays cautious/neutral. The classifier does not fabricate regime labels from unavailable inputs.
- It does not add order execution, private API access, withdrawals, transfers, or live Telegram sending by default.

## Phase 32 Performance Memory Layer

Phase 32 adds `app/analytics/performance_memory.py`, a deterministic local evidence layer that stores historical setup performance from real replay/backtest outcomes. It is not AI prediction, not black-box machine learning, and not trade execution. The scanner remains rule-based and auditable.

What it stores locally in `scan_runs/performance_memory.json`:

- Deterministic setup fingerprints built from stable conditions such as direction, HTF alignment, market regime, derivatives state, crowding, squeeze risk, RR bucket, pullback quality, OB/FVG quality, confirmation strength, volatility regime, symbol category, mode, and setup type.
- Historical samples, filled samples, wins, losses, TP1/TP2 hit rates, average R, median R, max drawdown, average hold time, rejection frequency, invalidation frequency, regime stats, symbol stats, and dedupe IDs for replay ingestion.
- Corrupted or impossible entries are rejected on load. Missing or insufficient history is shown as `N/A`, `insufficient_sample`, or `unverified`.

Confidence buckets:

- `<10` samples: `VERY_LOW`
- `10-24` samples: `LOW`
- `25-74` samples: `MEDIUM`
- `75-199` samples: `HIGH`
- `200+` samples: `VERY_HIGH`

How it affects scans:

- Performance memory may add a small bounded overlay to displayed edge/readiness/portfolio preference: max `+10` and max `-15`.
- `VERY_LOW` samples never apply an aggressive adjustment. If the configured minimum confidence is not met, the output says `Performance memory confidence too low.`
- It cannot make invalid setups valid, cannot bypass RR/risk/market-regime protections, and cannot override any strategy gate.
- Portfolio selection may prefer a valid setup with stronger historical expectancy or memory confidence, but invalid and near-miss setups remain unselected.

CLI flags:

- `--performance-memory`: force-enable memory for the scan.
- `--disable-performance-memory`: disable the layer and mark it disabled in output.
- `--reset-performance-memory`: reset `scan_runs/performance_memory.json`.
- `--min-memory-confidence VERY_LOW|LOW|MEDIUM|HIGH|VERY_HIGH`: choose the minimum bucket required before adjustments apply. Default is `LOW`.

Defaults:

- Replay/backtest scans automatically feed performance memory unless disabled.
- The daily command preset enables performance memory by default.
- Normal scans can read memory with `--performance-memory`.

Example replay learning flow:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT SOLUSDT --replay --performance-memory --output-json scan_runs/latest_scan.json
```

Example scanner output:

```text
Performance Memory
Samples: 42
Confidence: MEDIUM
Avg expectancy: +0.8R
Strongest regime: TREND_EXPANSION
Weakest regime: CHOP
Historical TP1: 63%
Historical TP2: 38%
```

JSON output adds per-symbol `performance_memory`, `historical_expectancy`, `confidence_bucket`, `memory_adjustments`, and `historical_warning`, plus a scan-level `performance_memory_summary` for the Daily Command Center.

Safety boundaries:

- Phase 32 uses only replay/backtest results, completed historical scans with outcomes, or stored replay summaries. It does not invent outcomes or probabilities.
- It stores memory locally only and does not call external ML services.
- It does not add live order execution, private exchange APIs, withdrawal or transfer functionality, or live Telegram sending.
- It does not weaken sweep, BOS/CHoCH, OB/FVG, fib, RR, Trust Meter, risk, scoring, portfolio, or market-regime gates.

## Phase 33 Structured Dataset & Scan History Database

Phase 33 adds a durable local SQLite dataset in `app/storage/`. The dataset is meant to become the analyzable asset for Candle Craft Intelligence: every stored scan can preserve scan metadata, symbol outcomes, valid setup candidates, replay/backtest outcomes, regime readings, quality scores, and portfolio decisions.

Default database location:

```text
scan_runs/candle_craft.db
```

Storage is opt-in. A normal scan does not write to the database unless `--store-scan` is provided.

Store a scan:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --command-preset daily --symbols BTCUSDT ETHUSDT SOLUSDT --store-scan
```

Use a custom database path:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT --store-scan --database-path scan_runs/candle_craft.db
```

When storage is enabled, the CLI prints:

```text
Stored scan run: [run_id]
Database: [path]
```

View recent history:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --show-history --history-limit 10
```

Export recent history:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --export-history-json scan_runs/history.json --history-limit 50
```

Stored scan runs include exchange, universe, symbols scanned, strategy, timeframes, market regime, regime confidence, compatibility scores, environment notes, runtime stats, command/preset, valid setup count, near misses, rejected symbols, and data issues. Symbol records include display bucket, readiness score, setup quality score, edge score, failed gate, rejection reason, next trigger, action label, regime state, regime compatibility, regime penalty, derivatives context, volume profile context, pullback status, and portfolio decision. Valid setups and replay outcomes are stored in separate tables for later analysis.

Safety boundaries:

- Phase 33 is local persistence only. It does not alter strategy gates, setup validation, risk validation, market-regime filtering, portfolio selection, or ranking logic.
- It does not add order execution, private exchange API access, withdrawals, transfers, account endpoints, or live Telegram sending.
- Missing data remains `N/A`; unreliable data remains `Unverified`.
- Storage writes scan outputs as produced by the existing dry-run scanner and does not invent market data or outcomes.

## Phase 34 Research & Analytics Query Layer

Phase 34 adds a read-only research layer in `app/research/` for analyzing the local Phase 33 SQLite scan history database. It turns stored scan, setup, near-miss, rejection, regime, quality, and replay records into research tables so you can see what has actually worked and what keeps failing.

Run a summary:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query summary --database-path scan_runs/candle_craft.db
```

Available research queries:

```text
summary
best_symbols
worst_symbols
best_regimes
worst_regimes
rejection_reasons
setup_quality
near_misses
replay_expectancy
mode_performance
symbol_detail
regime_expectancy
regime_setup_density
regime_rejection_patterns
regime_quality_distribution
watch_iterations
```

Examples:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query rejection_reasons --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query symbol_detail --research-symbol BTCUSDT --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query best_symbols --research-limit 20 --research-mode swing --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query best_regimes --research-regime trend_expansion --database-path scan_runs/candle_craft.db
```

Write JSON instead of console tables:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query replay_expectancy --research-output-json scan_runs/research_replay.json --database-path scan_runs/candle_craft.db
```

Data limitations:

- Research output only reflects scans that were explicitly stored with `--store-scan`.
- Replay expectancy, TP1, and TP2 rates only exist when scans were stored with replay results.
- Missing data remains `N/A`; unreliable data remains `Unverified`.
- When replay sample size is below the reliability threshold, reports show: `Sample size too small for reliable conclusion.`
- If the database is missing, the CLI prints: `No scan database found. Run scans with --store-scan first.`

Safety boundaries:

- Phase 34 is read-only analytics. It does not run scans, alter strategy gates, weaken setup logic, place orders, call private exchange APIs, send Telegram alerts, withdraw funds, or transfer funds.
- It does not invent market data, setups, or replay outcomes; unavailable metrics remain `N/A`.

## Phase 36 Setup Lifecycle / State Progression Engine

Phase 36 adds `app/lifecycle/`, a deterministic state engine that tracks each `symbol/mode/direction` setup across scan iterations. Instead of treating every scan as a fresh snapshot, Candle Craft can now persist where a setup is in its lifecycle and record the transition history that got it there.

Lifecycle is enabled by default when `--watch` or `--store-scan` is used. It can also be enabled explicitly with `--lifecycle`, disabled with `--disable-lifecycle`, shown with `--show-lifecycle`, and cleared with `--reset-lifecycle`.

Lifecycle states:

```text
DISCOVERED
REJECTED
WATCHLISTED
STALKING
TRIGGERED
CONFIRMED
EXECUTING
MANAGING
TP_HIT
SL_HIT
INVALIDATED
EXPIRED
COOLDOWN
ARCHIVED
```

Core progression:

```text
DISCOVERED -> WATCHLISTED -> STALKING -> TRIGGERED -> CONFIRMED -> EXECUTING -> MANAGING
MANAGING -> TP_HIT / SL_HIT / INVALIDATED / EXPIRED
INVALIDATED / TP_HIT / SL_HIT / EXPIRED -> COOLDOWN -> ARCHIVED
```

Transition rules:

- `REJECTED` can move to `WATCHLISTED` only when readiness improves.
- `WATCHLISTED` moves to `STALKING` when an execution sweep appears.
- `STALKING` moves to `TRIGGERED` when 5m BOS/CHoCH appears after the sweep.
- `TRIGGERED` moves to `CONFIRMED` when pullback zone and RR are valid.
- `CONFIRMED` moves to `EXECUTING` only when a valid trade idea already exists.
- `EXECUTING` moves to `MANAGING` only after an entry fill is simulated or confirmed by lifecycle input.
- `MANAGING` can move to `TP_HIT`, `SL_HIT`, `INVALIDATED`, or `EXPIRED`.
- No lifecycle can skip directly from `WATCHLISTED` to `EXECUTING`.

Persistence:

- Lifecycle state is stored in `scan_runs/candle_craft.db` when lifecycle is enabled.
- `setup_lifecycle_records` stores current state, previous state, timestamps, failed gate, readiness score, quality score, edge score, regime state, action label, invalidation reason, cooldown expiry, and archive time.
- `setup_lifecycle_events` stores every transition with timestamp, symbol, from/to state, reason, optional scan run id, readiness score, quality score, failed gate, and notes.

Display output includes a lifecycle block when state is attached:

```text
Lifecycle:
- State: TRIGGERED
- Previous: STALKING
- Transition: STALKING -> TRIGGERED
- Reason: 5m BOS/CHoCH confirmed after sweep.
- First seen: 2026-05-18T09:00:00+00:00
- Last updated: 2026-05-18T09:10:00+00:00
```

Watch mode behavior:

- Watch scans prioritize lifecycle states in this order: `STALKING`, `TRIGGERED`, `CONFIRMED`, `WATCHLISTED`.
- `ARCHIVED` and `COOLDOWN` records are not prioritized for watch mode.
- If lifecycle priority would empty an explicitly requested watchlist, the original requested symbols are preserved so a new structure can reactivate the setup.
- Telegram remains dry-run by default. Live Telegram still requires the explicit `--telegram-live-alerts true` path and credentials.

Lifecycle research queries:

```text
lifecycle_summary
lifecycle_transitions
lifecycle_conversion
lifecycle_funnel
lifecycle_dropoffs
lifecycle_symbol_conversion
lifecycle_state_duration
lifecycle_symbol_detail
```

Lifecycle metrics include `WATCHLISTED -> STALKING`, `STALKING -> TRIGGERED`, `TRIGGERED -> CONFIRMED`, `CONFIRMED -> EXECUTING`, and `EXECUTING -> TP_HIT / SL_HIT / INVALIDATED / EXPIRED` conversion rates, funnel counts, dropoff causes, symbol-level conversion, state duration, and stale lifecycle counts.

Examples:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT --store-scan --show-lifecycle

.\.venv\Scripts\python.exe scripts\run_scan.py --watch --symbols BTCUSDT ETHUSDT --show-lifecycle

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query lifecycle_conversion --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query lifecycle_funnel --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query lifecycle_dropoffs --lifecycle-stale-hours 12 --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query lifecycle_symbol_conversion --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query lifecycle_state_duration --lifecycle-stale-hours 24 --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query lifecycle_symbol_detail --research-symbol BTCUSDT --database-path scan_runs/candle_craft.db
```

### Phase 37 - Lifecycle Conversion Analytics

Purpose:

- Turn stored lifecycle history into conversion analytics that show where setups progress, where they stall, and which paths move toward valid or executable opportunities.
- Keep the analysis read-only. It reports lifecycle outcomes from `setup_lifecycle_records` and `setup_lifecycle_events`; it does not create trades, modify strategy gates, or delete stale records.

Available queries:

- `lifecycle_conversion` summarizes total, active, archived, funnel counts, conversion rates, outcomes, dropoffs, duration stats, stale lifecycles, and per-symbol conversion.
- `lifecycle_funnel` prints the main funnel: `DISCOVERED`, `WATCHLISTED`, `STALKING`, `TRIGGERED`, `CONFIRMED`, `EXECUTING`, `TP_HIT`, `SL_HIT`, `INVALIDATED`, and `ARCHIVED`.
- `lifecycle_dropoffs` shows the biggest dropoff stage, common failed gates, common invalidation reasons, average readiness and quality at dropoff, and regime state when available.
- `lifecycle_symbol_conversion` shows lifecycle count, highest state reached, conversion to `CONFIRMED`, conversion to `EXECUTING`, average time to highest state, and most common failure point for each symbol.
- `lifecycle_state_duration` shows average and median time in each state, longest stuck symbols, stale lifecycle count, and stale lifecycle details.

Interpretation examples:

- High `WATCHLISTED -> STALKING` but low `STALKING -> TRIGGERED` means sweeps are appearing, but confirmation structure is not completing.
- High `TRIGGERED -> CONFIRMED` but low `CONFIRMED -> EXECUTING` means pullback/RR setups are forming, but valid trade ideas are not consistently appearing.
- A common `rr_below_minimum` dropoff means the setup may have structure, but reward-to-risk is not compensating for the risk.
- Stale `WATCHLISTED`, `STALKING`, `TRIGGERED`, or `CONFIRMED` lifecycles are reported after `--lifecycle-stale-hours`; no records are deleted by default.

Sample size warnings:

- Treat small lifecycle samples as exploratory only. A handful of paths can identify failure points, but they are not enough to prove an edge.
- Prefer comparing conversion rates after enough repeated scans across regimes, modes, and symbols.
- Missing data is reported as `N/A`; unreliable data remains `Unverified`. Do not infer market data that was not recorded.

Safety boundaries:

- Phase 36 tracks state only. It does not weaken strategy gates, create valid setups from invalid setups, or bypass existing quality/risk checks.
- Phase 37 is analytics only. It does not weaken setup rules, change strategy logic, create trades from invalid states, add order execution, add private exchange API access, or send live Telegram by default.
- It does not add order execution, private exchange API access, withdrawals, transfers, account endpoints, or live Telegram sending.
- `EXECUTING` and `MANAGING` are lifecycle states only; they do not place orders.
- Missing data remains `N/A`; unreliable data remains `Unverified`; lifecycle never invents market data or outcomes.

## Phase 38 - Pullback Structure Intelligence

Purpose:

- Add read-only diagnostics for why pullbacks fail after sweep and BOS/CHoCH, especially at the `TRIGGERED` lifecycle stage.
- Keep the existing strategy strictness intact. Phase 38 does not loosen pullback depth, OB/FVG, fib, RR, Trust Meter, risk, or regime gates.
- Make `pullback_too_deep`, `no_ob_or_fvg_zone`, and `rr_below_minimum` easier to research without converting failed setups into valid setups.

New module:

- `app/analytics/pullback_intelligence.py`
- Models: `PullbackIntelligenceInput`, `PullbackIntelligenceResult`, `PullbackFailureType`, `PullbackQualityGrade`, and `PullbackProjection`.

Failure types:

```text
TOO_DEEP
TOO_SHALLOW
NO_OB_FVG
FIB_MISALIGNMENT
LATE_PULLBACK
WEAK_DISPLACEMENT
OPPOSING_STRUCTURE_BLOCK
RR_COMPRESSION
DATA_INCOMPLETE
```

Pullback intelligence fields include:

```text
pullback_depth_ratio
fib_zone_status
ob_fvg_status
displacement_strength
candles_since_bos
freshness_score
rr_potential_score
structure_risk_score
pullback_quality_grade
pullback_failure_type
next_pullback_condition
```

Quality grades:

- `A`: ideal pullback.
- `B`: acceptable but imperfect.
- `C`: weak or watch only.
- `REJECT`: invalid pullback.
- `N/A`: insufficient data.

Lifecycle behavior:

- `TOO_DEEP` means the pullback exceeded 0.786. The scanner marks intent as weakened before entry, requires a fresh sweep plus BOS/CHoCH, and prevents same-structure reactivation. Existing `TRIGGERED` lifecycles move to `INVALIDATED` under the current lifecycle rules.
- `NO_OB_FVG` means there is no clean execution zone inside displacement. It can remain watchlisted or triggered only when sweep and BOS/CHoCH are already confirmed; activation still requires a valid OB/FVG.
- `RR_COMPRESSION` means target distance is not worth the risk. It can remain watchlisted, but it cannot become confirmed until RR improves through a better entry, wider clean TP2, or new structure.
- `DATA_INCOMPLETE` keeps fields as `N/A`; the scanner does not infer missing market data.

Display:

Near-miss and visible rejected cards can show:

```text
Pullback Intelligence
- Failure type: TOO_DEEP
- Depth: 0.82
- Fib status: failed
- OB/FVG: missing
- Freshness: weak
- RR potential: low
- Next condition: fresh sweep + BOS required
```

Use `--show-pullback-details` to print the same pullback block for visible compact/non-full output.

Research queries:

```text
pullback_failures
pullback_quality_distribution
pullback_depth_analysis
pullback_lifecycle_dropoffs
```

Research output includes failure type counts, average depth by failure type, most common failed symbols, failure by regime, failure by lifecycle state, and conversion rate by pullback grade.

Examples:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --symbols BTCUSDT ETHUSDT --show-pullback-details

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query pullback_failures --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query pullback_quality_distribution --database-path scan_runs/candle_craft.db

.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query pullback_depth_analysis --database-path scan_runs/candle_craft.db
```

Safety boundaries:

- Phase 38 is intelligence, diagnostics, and research only.
- It does not weaken strategy gates, lower RR requirements, allow pullbacks beyond invalidation limits, or create valid setups from failed pullbacks.
- It does not add order execution, private exchange API access, withdrawals, transfers, account endpoints, or live Telegram sending.
- Missing data remains `N/A`; unreliable data remains `Unverified`; market data is never invented.

## Phase 39 - Adaptive Symbol Prioritization

Purpose:

- Improve large universe and overnight scans by spending time on symbols that have produced reliable, useful scanner states.
- Reduce repeated public API timeouts by temporarily cooling down problem symbols instead of permanently banning them.
- Keep strategy gates unchanged. Symbol health only changes scan order or temporary skip behavior.

Symbol health is stored in SQLite table `symbol_health` and in scan JSON under `symbol_health`. Per symbol it tracks successful scans, timeout count, data issue count, average runtime, last success, last timeout, health score, cooldown, timeout strikes, priority rank, and prior usefulness.

Health scoring:

- Scores are bounded from `0` to `100`.
- Repeated timeouts and data issues reduce score.
- Stable successful scans increase score.
- Prior useful lifecycle states, `HOT WATCH`, near-miss, confirmed, triggered, and executing states increase score.
- Low-health and stale rejected symbols are scanned later unless lifecycle priority overrides them.

Cooldown behavior:

- `--max-timeout-strikes` controls how many repeated timeouts trigger cooldown.
- `--symbol-cooldown-minutes` controls temporary cooldown length.
- Cooldown symbols are skipped until expiry, then become eligible again. They are not permanently banned.

Queue priority order:

1. Lifecycle `CONFIRMED`, `EXECUTING`, `TRIGGERED`, or `MANAGING`.
2. `HOT WATCH`, `VALID SETUP`, near-miss, or watchlisted symbols.
3. Higher symbol health score.
4. Original universe order, which preserves liquidity or market-cap rank from the universe resolver.
5. Previously useful symbols.

CLI flags:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --universe binance_usdt_perp_top_volume --universe-size 200 --adaptive-symbol-priority --show-symbol-health --store-scan

.\.venv\Scripts\python.exe scripts\run_scan.py --watch --watch-symbols-from-latest-run --symbol-cooldown-minutes 30 --max-timeout-strikes 3
```

Defaults:

- Adaptive priority is enabled automatically for `--watch`, `--universe-size >= 100`, or resolved watchlists of at least 100 symbols.
- Smaller manual scans keep the previous input order unless `--adaptive-symbol-priority` is passed. `--show-symbol-health` displays the health block without changing queue order by itself.

Dashboard output includes:

```text
Symbol Health
Prioritized symbols: 198
Cooldown symbols: 2
Timeout strikes this run: 1
Slowest symbols: [SLOWUSDT 12.5s]
Skipped due to cooldown: 2
```

Research queries:

```text
symbol_health
slow_symbols
timeout_symbols
priority_symbols
```

Overnight recommendation:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --universe binance_usdt_perp_top_volume --universe-size 200 --store-scan --save-run scan_runs\latest_scan.json --progress --adaptive-symbol-priority --symbol-cooldown-minutes 30 --max-timeout-strikes 3
```

Safety boundaries:

- Phase 39 is reliability and prioritization only.
- It does not weaken sweep, BOS/CHoCH, pullback, OB/FVG, fib, RR, Trust Meter, risk, setup quality, portfolio selection, or regime gates.
- It does not create trades from invalid setups, add order execution, add private exchange API access, add withdrawals or transfers, or send live Telegram by default.

## Phase 40 - Graceful Watch Shutdown & Iteration Persistence

Purpose:

- Make long-running watch mode clean to stop with `Ctrl+C`.
- Persist every completed watch iteration as a scan run when `--store-scan` is enabled.
- Make research summaries and watch-specific research reflect actual stored watch iterations.

Safe stopping:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --watch --watch-symbols-from-latest-run --store-scan --watch-interval-sec 60
```

Press `Ctrl+C` while watch mode is sleeping between iterations. The CLI exits cleanly without an asyncio traceback and prints:

```text
Watch mode stopped by user.
Completed iterations: X
Stored scan runs: X
Data saved to: scan_runs/candle_craft.db
```

Watch iteration storage:

- Storage remains opt-in with `--store-scan`.
- Each completed watch iteration creates one row in `scan_runs`.
- Stored watch fields include iteration number, start/completion timestamps, requested/queued/completed symbol counts, valid activations, still-watching count, rejected/no-edge count, data issues, runtime seconds, market regime, portfolio summary when available, and symbol health summary when available.
- Existing `scan_runs/candle_craft.db` files are migrated in place with backward-compatible columns.
- If `--store-scan` is not used, watch mode does not create a scan run for the iteration.

Research:

```powershell
.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query summary
.\.venv\Scripts\python.exe scripts\run_scan.py --research --research-query watch_iterations
```

The summary report now includes total watch iterations, last watch iteration, average symbols per watch iteration, and valid activations from watch mode. The `watch_iterations` query prints iteration number, timestamp, symbols watched, valid activations, still watching, data issues, runtime, and regime.

Safety boundaries:

- Phase 40 is operational reliability and local persistence only.
- It does not change strategy gates, weaken setup logic, create trades from invalid setups, add order execution, add private exchange API access, add withdrawals or transfers, or send live Telegram by default.

## Phase 41 - Wick vs Close Structural Intelligence

Purpose:

- Distinguish liquidity wicks from body-close acceptance beyond structural fib limits.
- Avoid invalidating a pullback only because a wick swept beyond 0.786 when the candle closes back inside the valid side and structure remains intact.
- Keep hard gates intact: OB/FVG, fib entry alignment, RR, Trust Meter, risk, regime, and final strategy gates still decide whether anything can become valid.

Why wick vs close matters:

- A wick beyond 0.786 can be a liquidity sweep if price reclaims quickly.
- A body close beyond 0.786 is acceptance, not just a sweep, and is treated as higher risk.
- Multiple closes beyond the invalidation zone or a close through the structure edge means the structure is broken.

Acceptance classifications:

- `CLEAN_PULLBACK`: wick and close stay within the accepted structure.
- `WICK_SWEEP_RECLAIM`: wick breaches 0.786, but close reclaims the valid side with weak reclaim quality. This is watch-only.
- `DEEP_RECLAIM_VALID`: wick breaches 0.786, close reclaims strongly, and structure remains intact. This allows continued tracking but does not bypass any gate.
- `BODY_ACCEPTANCE_FAILURE`: a candle body closes beyond 0.786. This blocks confirmation from the current pullback.
- `STRUCTURAL_BREAKDOWN`: multiple closes accept beyond the invalidation zone or structure breaks. Lifecycle moves toward invalidation and cooldown.
- `DATA_INCOMPLETE`: required wick/close data is unavailable; fields remain `N/A`.

Lifecycle behavior:

- `WICK_SWEEP_RECLAIM` can remain `STALKING` or `TRIGGERED`, but it does not create a valid setup.
- `DEEP_RECLAIM_VALID` can remain watch/near-miss while OB/FVG, RR, and final gates are evaluated normally.
- `BODY_ACCEPTANCE_FAILURE` invalidates the current pullback context.
- `STRUCTURAL_BREAKDOWN` invalidates the context and can progress to `COOLDOWN`.

Display:

When `--show-pullback-details` is enabled, pullback output includes:

```text
Wick/Close Structure
- Wick depth: 0.82
- Close depth: 0.76
- Acceptance: WICK_SWEEP_RECLAIM
- Reclaim: weak
- Candles below zone: 0
- Structural status: intact
```

JSON:

Scanner JSON includes `wick_close_structure`, `acceptance_status`, `reclaim_strength`, and `body_acceptance_ratio` in pullback intelligence output and display metadata.

Research queries:

```text
wick_close_failures
acceptance_status_distribution
reclaim_quality_analysis
```

Examples:

- Wick sweeps below 0.786 and closes back above it with weak reclaim: watch-only `WICK_SWEEP_RECLAIM`.
- Wick sweeps deeply and closes back strongly with BOS/CHoCH still intact: `DEEP_RECLAIM_VALID`, but OB/FVG and RR still must pass.
- One body close below 0.786: `BODY_ACCEPTANCE_FAILURE`.
- Multiple body closes below 0.786: `STRUCTURAL_BREAKDOWN`.

Safety boundaries:

- Phase 41 is structural diagnostics and classification only.
- It does not lower RR requirements, create trades from invalid structures, bypass OB/FVG or Trust Meter gates, place orders, use private API access, add withdrawals/transfers, or send live Telegram by default.
- Missing data remains `N/A`; unreliable data remains `Unverified`; market data is never invented.

## Phase 42 - Dynamic RR & Target Intelligence

Purpose:

- Improve target selection diagnostics so the scanner can explain whether a setup has real profit room before RR rejection.
- Keep existing RR hard gates unchanged. A setup with TP2 below the required RR remains rejected or near-miss only.
- Map visible target room from available candle structure, not from arbitrary R-multiple expansion.

Target detection hierarchy:

1. Nearest opposing swing high or swing low.
2. Equal highs or equal lows.
3. Recent range high or range low.
4. Prior BOS origin when it is on the target side of entry.
5. HTF supply/demand proxy from higher-timeframe candles when available.
6. Volume Profile VAH, VAL, POC, and nearest profile nodes when available.
7. Fib extensions from a real impulse only: `1.272`, `1.618`, and `2.0`.

RR compression:

- `RR_BELOW_MINIMUM` means TP2 exists, but RR to TP2 remains below the mode requirement.
- `TP_TOO_CLOSE` means only a close target is visible or TP2 is too near the entry for the current stop.
- `OPPOSING_STRUCTURE_BLOCK` means a swing, equal level, range edge, BOS origin, or supplied liquidity level blocks clean path before required RR.
- `TARGET_INSIDE_CHOP` means the available target remains inside recent range/chop.
- `HTF_RESISTANCE_TOO_CLOSE` means a higher-timeframe supply/demand proxy is too close for clean RR.
- `NO_CLEAR_TARGET` means target intelligence cannot map a valid TP from observed structure.
- `DATA_INCOMPLETE` means required candle, entry, stop, or direction data is `N/A`.

Display output:

```text
Target Intelligence
- TP1 candidate: 106
- TP2 candidate: 108.09
- Clean path: 6
- RR to TP1: 1.2
- RR to TP2: 1.618
- Target quality: Reject
- Failure: RR_BELOW_MINIMUM
- Next condition: Wider clean TP2 or better entry required
```

Research queries:

```text
target_failures
rr_compression_analysis
target_quality_distribution
best_target_conditions
```

Safety boundaries:

- Phase 42 is diagnostics, target mapping, display, JSON, and research only.
- It does not lower RR requirements, weaken strategy gates, invent wider targets, create valid trades from invalid structures, place orders, use private exchange API access, add withdrawals or transfers, or send live Telegram by default.
- Missing data remains `N/A`; unreliable data remains `Unverified`; market data is never invented.

## Safety Boundaries

- No secrets are committed. Use `.env` locally and `.env.example` for documentation.
- Trade ideas must include invalidation and a risk warning.
- The `trades` table is for manual or paper records, not live order execution.
- Market data must be stored as observed. Missing data is `N/A`; unreliable data is `Unverified`.
- Opportunity scoring is scoring only. It does not create exchange orders or use private exchange access.
- Trade idea generation structures scored candidates only. It does not send alerts, place trades, or use private exchange access.
- Alert delivery is notification-only. It does not place orders, use private exchange access, or add trading execution.
- Journal tracking is in-memory outcome tracking only. It does not place orders, use private exchange access, or persist records yet.
- The scanner runner is orchestration only. It does not place orders, use private exchange access, or create a trade idea unless all configured quality gates pass.
- The liquidity-grab pullback engine is strategy analysis and formatting only. It does not place orders, use private exchange access, or convert a setup into live execution.
- The Phase 12 scanner strategy path is still dry-run analysis only. It can format a dry-run alert after all gates pass, but rejected setups remain diagnostics only.
- The Phase 13 volume profile is candle-estimated confluence only. It does not create signals by itself, place orders, or use private exchange API access.
- The Phase 14 pullback-zone engine is deterministic validation only. It does not place orders, use private exchange API access, or loosen any sweep, confirmation, pullback, fib, RR, or Trust Meter gates.
- The Phase 15 derivatives enrichment layer is public-data confluence only. It does not create setups, place orders, use private exchange API access, or loosen any technical strategy gate.
- The Phase 15.2 confirmation-to-pullback fix is index propagation only. It does not add order execution, private exchange API access, withdrawals, transfers, or account endpoints.
- The Phase 16 Telegram formatter is text output only. It does not send live Telegram messages, create alerts, place orders, use private exchange API access, withdrawals, transfers, account endpoints, or loosen any strategy gate.
- The Phase 17 premium scanner display is output-only. It does not send live Telegram messages, create fake setups, place orders, use private exchange API access, withdrawals, transfers, account endpoints, or loosen any sweep, BOS/CHoCH, OB/FVG, pullback, fib, RR, scoring, or risk gate.
- The Phase 18 scanner result ranking layer is output-only. It does not create trades, change strategy gate strictness, place orders, use private exchange API access, withdrawals, transfers, account endpoints, or modify scan artifacts by itself.
- The Phase 19 watchlist preset layer resolves symbol inputs only. It does not create trades, change strategy gate strictness, place orders, use private exchange API access, withdrawals, transfers, account endpoints, live Telegram sending, or modify scan artifacts by itself.
- The Phase 20 cache/resume layer is public-data reliability only. It does not cache private/account data, create trades, change strategy gate strictness, place orders, use private exchange API access, withdrawals, transfers, account endpoints, or send live Telegram messages.
- The Phase 21 symbol universe layer resolves public scanner inputs only, including optional public market-cap rankings for the market-cap universe. It does not cache private/account data, create trades, change strategy gate strictness, place orders, use private exchange API access, withdrawals, transfers, account endpoints, or send live Telegram messages.
- The Phase 22 near-miss intelligence layer is output-only. It does not create trade ideas from near-misses, send Telegram alerts, change strategy gate strictness, place orders, use private exchange API access, withdrawals, transfers, or account endpoints.
- The Phase 23 setup quality layer is post-strategy validation only. It does not weaken strategy gates, create trades from invalid setups, place orders, use private exchange API access, withdrawals, transfers, account endpoints, or send live Telegram messages.
- The Phase 24 historical replay layer is diagnostic candle replay only. It does not weaken live strategy gates, replace live ranking, place orders, call private exchange APIs, send live Telegram messages, withdraw funds, or transfer funds.
- The Phase 28 portfolio selection layer is output-side selection and risk intelligence only. It does not weaken strategy gates, create invalid trades, place orders, call private exchange APIs, send live Telegram messages, withdraw funds, or transfer funds.
- The Phase 29 alert watch mode is repeat scanning and notification gating only. It does not weaken strategy gates, create trades from near-misses, place orders, call private exchange APIs, send live Telegram messages by default, withdraw funds, or transfer funds.
- The Phase 31 market regime filter is a public-data, scan-level overlay only. It does not weaken strategy gates, create trades from invalid setups, place orders, call private exchange APIs, send live Telegram messages by default, invent unavailable market data, withdraw funds, or transfer funds.
- The Phase 32 performance memory layer is local historical evidence only. It does not predict, fabricate statistics, weaken gates, create valid setups from invalid setups, place orders, call private exchange APIs, send live Telegram messages by default, withdraw funds, or transfer funds.
- The Phase 33 scan history database is local persistence only. It does not change strategy gates, place orders, call private exchange APIs, send live Telegram messages by default, invent unavailable market data, withdraw funds, or transfer funds.
- The Phase 34 research query layer is read-only analytics only. It does not change strategy gates, place orders, call private exchange APIs, send live Telegram messages by default, invent unavailable market data, withdraw funds, or transfer funds.
- The Phase 35 regime intelligence layer is an environment filter only. It can reject or downgrade weak environments, but it cannot create setups, bypass strategy gates, invent unavailable regime inputs, place orders, call private APIs, send live Telegram messages by default, withdraw funds, or transfer funds.
- The Phase 36 lifecycle engine is state tracking only. It does not weaken strategy gates, create valid setups from invalid setups, place orders, call private APIs, send live Telegram messages by default, invent market data or outcomes, withdraw funds, or transfer funds.
- The Phase 37 lifecycle conversion analytics layer is read-only reporting only. It does not weaken setup rules, create trades from invalid states, place orders, call private APIs, send live Telegram messages by default, delete lifecycle records, invent market data, withdraw funds, or transfer funds.
- The Phase 38 pullback intelligence layer is diagnostics and research only. It does not loosen pullback, fib, OB/FVG, RR, Trust Meter, risk, or lifecycle gates; create valid setups from failed pullbacks; place orders; call private APIs; send live Telegram by default; invent data; withdraw funds; or transfer funds.
- The Phase 39 symbol health layer is scan ordering and timeout hygiene only. It does not weaken strategy gates, create trades from invalid setups, place orders, call private APIs, send live Telegram by default, invent data, withdraw funds, or transfer funds.
- The Phase 40 watch shutdown and persistence layer is operational reliability only. It does not weaken strategy gates, create trades from invalid setups, place orders, call private APIs, send live Telegram by default, invent data, withdraw funds, or transfer funds.
- The Phase 41 wick-vs-close structural intelligence layer is diagnostics and classification only. It does not weaken RR, OB/FVG, fib, Trust Meter, risk, or final strategy gates; place orders; call private APIs; send live Telegram by default; invent data; withdraw funds; or transfer funds.
- The Phase 42 target intelligence layer is diagnostics and research only. It does not lower RR requirements, weaken strategy gates, invent targets, create valid trades from invalid structures, place orders, call private APIs, send live Telegram by default, withdraw funds, or transfer funds.
- The Phase 43 local runtime diagnostics are environment checks only. They do not call live exchange APIs, send Telegram messages, place orders, use private exchange API access, invent market data, withdraw funds, or transfer funds.
- The Phase 44A scan persistence audit is read-only artifact inspection only. It does not mutate scan outputs, alter gates, call exchanges, send Telegram messages, place orders, invent market data, withdraw funds, or transfer funds.
- The Phase 44B lifecycle replay readiness audit is read-only artifact inspection only. It does not mutate lifecycle state, watch state, scan history, performance memory, scanner results, trade ideas, alerts, or database records; it does not execute trades, alter gates, call exchanges, send Telegram messages, invent market data, withdraw funds, or transfer funds.
- The Phase 44C replay dataset export contract is read-only research export only. It does not execute replay, create signals, place trades, call exchanges, send Telegram messages, alter setup gates, mutate lifecycle state, update performance memory, invent market data, withdraw funds, or transfer funds.
- The Phase 44D replay dataset quality metrics layer is read-only audit/research only. It scores data and replay-readiness quality only; it does not calculate profitability, create signals, place trades, call exchanges, send Telegram messages, mutate artifacts, alter gates, invent market data, withdraw funds, or transfer funds.
