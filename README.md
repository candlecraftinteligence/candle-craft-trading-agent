# Candle Craft Trading Agent

Phase 1 foundation for a crypto trading intelligence system, with Phase 2 public market-data clients, Phase 3 technical structure analysis, Phase 4 derivatives/orderflow context analysis, Phase 5 risk-management validation, Phase 6 opportunity scoring, Phase 7 structured trade ideas, Phase 8 dry-run-first alert formatting, Phase 9 in-memory journal tracking, Phase 10 scanner-runner orchestration, and Phase 11 liquidity-grab pullback strategy analysis.

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
|   |-- alerts/
|   |-- api/
|   |-- core/
|   |-- data/
|   |   |-- exchange_clients/
|   |   `-- normalizers/
|   |-- db/
|   |-- models/
|   |-- pipeline/
|   |-- scoring/
|   `-- strategies/
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

The tests cover settings loading, the FastAPI health endpoint, model metadata imports, mocked public market-data client responses, deterministic analysis agents, risk validation, opportunity scoring, structured trade idea generation, mocked alert delivery behavior, in-memory journal tracking, the Phase 10 scanner runner, and the Phase 11 liquidity-grab pullback engine. Tests do not call live exchange APIs or live Telegram APIs.

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

- `challenge`: strictest mode. Requires Trust Meter >= 85, RR >= 3.0, fixed 5% risk text, limit pullback entries only, no meme/illiquid token classification, and no active BTC/event guard when provided. Invalid Challenge output exposes the exact message `No valid challenge setup.`
- `swing`: uses 1h or 4h execution candles where available, with 15m fallback, and applies the base RR >= 2.5 gate.
- `scalp`: uses 15m execution candles where available, with 5m fallback, and requires the pullback entry to remain valid within the short LTF window.

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
