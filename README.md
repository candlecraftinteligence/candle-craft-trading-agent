# Candle Craft Trading Agent

Phase 1 foundation for a crypto trading intelligence system, with Phase 2 public market-data clients, Phase 3 technical structure analysis, Phase 4 derivatives/orderflow context analysis, Phase 5 risk-management validation, Phase 6 opportunity scoring, and Phase 7 structured trade ideas.

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
|   |-- api/
|   |-- core/
|   |-- data/
|   |   |-- exchange_clients/
|   |   `-- normalizers/
|   |-- db/
|   |-- models/
|   `-- scoring/
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

The tests cover settings loading, the FastAPI health endpoint, model metadata imports, mocked public market-data client responses, deterministic analysis agents, risk validation, opportunity scoring, and structured trade idea generation. Tests do not call live exchange APIs.

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

## Safety Boundaries

- No secrets are committed. Use `.env` locally and `.env.example` for documentation.
- Trade ideas must include invalidation and a risk warning.
- The `trades` table is for manual or paper records, not live order execution.
- Market data must be stored as observed. Missing data is `N/A`; unreliable data is `Unverified`.
- Opportunity scoring is scoring only. It does not create exchange orders or use private exchange access.
- Trade idea generation structures scored candidates only. It does not send alerts, place trades, or use private exchange access.
