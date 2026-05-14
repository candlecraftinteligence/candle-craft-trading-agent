# Candle Craft Trading Agent

Phase 1 foundation for a crypto trading intelligence system, with Phase 2 public market-data clients, Phase 3 technical structure analysis, Phase 4 derivatives/orderflow context analysis, and Phase 5 risk-management validation.

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
|   `-- models/
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

The tests cover settings loading, the FastAPI health endpoint, model metadata imports, and mocked public market-data client responses. Tests do not call live exchange APIs.

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

## Safety Boundaries

- No secrets are committed. Use `.env` locally and `.env.example` for documentation.
- Trade ideas must include invalidation and a risk warning.
- The `trades` table is for manual or paper records, not live order execution.
- Market data must be stored as observed. Missing data is `N/A`; unreliable data is `Unverified`.
