# Candle Craft Trading Agent

Phase 1 foundation for a crypto trading intelligence system.

This project is intentionally not an auto-trading bot. It does not place orders, does not expose exchange trading endpoints, and does not include withdrawal or transfer functionality. The initial scope is a modular backend foundation for market data, technical features, catalysts, trade ideas, alerts, manual or paper trade records, journal entries, and backtest metadata.

## Stack

- Python 3.11+
- FastAPI
- PostgreSQL
- SQLAlchemy 2.x
- Alembic
- Pydantic settings
- pytest
- Docker Compose

## Project Structure

```text
.
├── alembic/
├── app/
│   ├── api/
│   ├── core/
│   ├── db/
│   └── models/
├── tests/
├── docker-compose.yml
├── requirements.txt
├── pytest.ini
├── .env.example
├── AGENTS.md
└── README.md
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
pytest
```

The baseline tests cover settings loading, the FastAPI health endpoint, and model metadata imports.

## Safety Boundaries

- No secrets are committed. Use `.env` locally and `.env.example` for documentation.
- Trade ideas must include invalidation and a risk warning.
- The `trades` table is for manual or paper records, not live order execution.
- Market data must be stored as observed. Missing data is `N/A`; unreliable data is `Unverified`.
