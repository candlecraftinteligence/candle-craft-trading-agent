# Candle Craft Trading Agent

Phase 1 foundation for a crypto trading intelligence system, with Phase 2 public market-data clients.

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

## Safety Boundaries

- No secrets are committed. Use `.env` locally and `.env.example` for documentation.
- Trade ideas must include invalidation and a risk warning.
- The `trades` table is for manual or paper records, not live order execution.
- Market data must be stored as observed. Missing data is `N/A`; unreliable data is `Unverified`.
