# CCI The Pack

Telegram-native discipline layer beside CCI. This tree is the Phase 0 prototype: shell, mock missions, device-local locks and journals, Replay drills, and a cosmetic Pack XP preview. It does not connect to CCI Runtime, does not execute orders, and does not implement Stars, wallets, auth, or a server XP ledger.

Product decisions live in [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md).

Tagline: **Profit is the outcome. Discipline is the game.**

## Run the API

From `pack/apps/api`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

- `GET /health`
- `GET /api/missions`
- `GET /api/missions/{cci_setup_id}`

`CCI_SOURCE` must stay `mock`. Any other value refuses to start. Fixtures load from `pack/fixtures/missions` (override with `PACK_FIXTURES_DIR`).

## Run the web app

From `pack/apps/web`, with the API already on port 8000:

```bash
npm install
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` and `/health` to the API. Outside Telegram the shell shows a non-production browser banner. Dev and staging builds show a Mock / Training badge.

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

## Docker

From `pack/`:

```bash
docker compose up --build
```

The API container is mock-only. The web service is the Vite dev server.

## Safety

- New Pack code stays under `pack/`.
- Mission timelines render fixture events only.
- Hunt is a quality-tier badge on a Mission, not a separate object.
- Copy does not promise profit.
