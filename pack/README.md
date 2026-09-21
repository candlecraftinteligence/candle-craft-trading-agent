# CCI The Pack

Telegram-native discipline layer beside CCI. Phase 0 is the visual baseline. Phase 1 Slice A adds Postgres, Telegram initData auth, immutable decision locks, and a server Pack XP ledger. Missions still come from mock fixtures. The app does not connect to CCI Runtime, does not execute orders, and does not implement Stars or wallets. `LIVE_CCI` stays false. `CCI_SOURCE` stays `mock`.

Product decisions live in [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md).

Tagline: **Profit is the outcome. Discipline is the game.**

## Run with Docker Compose

From `pack/`, with Docker available:

```bash
docker compose up --build
```

Compose starts Postgres 16, runs `alembic upgrade head`, then the API. The web service is the Vite dev server on port 5173. The API is mock-only: `CCI_SOURCE=mock` and `LIVE_CCI=false`. Browser preview uses `POST /api/auth/dev`, which stays off when `PACK_ENV` or `APP_ENV` is `production`.

## Run the API without Compose

Postgres must already be listening, and `DATABASE_URL` must point at it. From `pack/apps/api`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp ../../.env.example ../../.env
set -a && source ../../.env && set +a
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

- `GET /health` reports `live_cci` as `false`
- `GET /api/missions`
- `GET /api/missions/{cci_setup_id}`
- `POST /api/auth/telegram` verifies Telegram `initData`
- `POST /api/missions/{cci_setup_id}/decision` locks one call
- `GET /api/me` reads Pack XP and Wolf Rank from the ledger

`CCI_SOURCE` must stay `mock`. Any other value refuses to start. `LIVE_CCI=true` refuses to start. Fixtures load from `pack/fixtures/missions` (override with `PACK_FIXTURES_DIR`).

## Run the web app

From `pack/apps/web`, with the API already on port 8000:

```bash
npm install
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` and `/health` to the API. Outside Telegram the shell shows a non-production browser banner and the API issues a synthetic dev session only when `DEV_BROWSER_MODE` is on and the environment is not production. Dev and staging builds show a Mock / Training badge. Profile Pack XP and Wolf Rank come from the server ledger.

```bash
npm run typecheck
npm run test
npm run build
```

Demo path: Pack den → Walk the board → open a card → seal one of TRACK, I TOOK THIS, WATCH ONLY, or NO TRADE → on a resolved card, save Your Journal beside the CCI Outcome → Run the tape → study a concealed setup → Reveal → Profile.

## Tests

From `pack/apps/api` with the virtualenv active:

```bash
python -m pytest
```

From `pack/apps/web`:

```bash
npm run test
```

## Safety

- New Pack code stays under `pack/`.
- `CCI_SOURCE` stays `mock`. `LIVE_CCI` stays false.
- Mission timelines render fixture events only.
- Hunt is a quality-tier badge on a Mission, not a separate object.
- Copy does not promise profit.
