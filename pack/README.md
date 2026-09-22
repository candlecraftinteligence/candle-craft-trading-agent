# CCI The Pack

Telegram-native discipline layer beside CCI. Phase 0 is the visual baseline. Phase 1 persists decisions, journals, Replay attempts, quests, and achievements in Postgres. Missions still come from mock fixtures. The app does not connect to CCI Runtime, does not execute orders, and does not implement Stars or wallets. `LIVE_CCI` stays false. `CCI_SOURCE` stays `mock`.

Status: **NEAR_COMPLETE_AWAITING_TELEGRAM_SECRETS**. The closeout is [docs/MVP_IMPLEMENTATION_REPORT.md](docs/MVP_IMPLEMENTATION_REPORT.md). Staging steps that do not need a bot token are in [docs/STAGING.md](docs/STAGING.md).

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
- `GET /api/me` reads Pack XP, Wolf Rank, and notification prefs from the server
- `GET /api/quests` returns today's drills and this week's drills, then awards quest XP once
- `GET /api/achievements` returns unlocked discipline marks
- `GET /api/replay` and `GET /api/missions/{cci_setup_id}/replay` return a concealed tape and the Replay disclaimer
- `POST /api/missions/{cci_setup_id}/replay` reveals the symbol, outcome, teaching note, and server score
- `POST /api/telegram/webhook` checks `X-Telegram-Bot-Api-Secret-Token` when `PACK_WEBHOOK_SECRET` is set, and returns 503 when it is empty

Missions → MINE lists setups the signed-in user has locked. It does not read a device-only decision list.

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

## Pack bot

The Pack bot is separate from the CCI production bot. It reads `PACK_TELEGRAM_BOT_TOKEN` only. Leave that empty and the process exits without taking the API down. Set `BOT_REQUIRED=true` when a missing token should stop boot.

```bash
cd pack/apps/bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=../api
export PACK_MINI_APP_URL=https://your-pack-host
export PACK_TELEGRAM_BOT_TOKEN=   # Pack BotFather token, not the CCI bot
python main.py
```

`/start` sends a short welcome and an Open The Pack button. A mission id on the command, or `https://t.me/<pack_bot>/app?startapp=<mission_id>`, opens Mission Detail. Unknown ids land on the not-found state. Compose can start the same process with `docker compose --profile bot up`. Without a public HTTPS Mini App URL and a Pack BotFather token, the bot stays scaffolding.

`EXTERNAL_BLOCKER` for live Telegram staging is only:

- `PACK_TELEGRAM_BOT_TOKEN`
- `PACK_MINI_APP_URL` on HTTPS

The webhook route is a verifier. It does not deliver updates until those values exist. See [docs/STAGING.md](docs/STAGING.md).

Notification types live on the user: new mission and resolution start on; quest, streak, and Replay nudges start off. Profile → Den signals toggles them. The sender does not invent urgency and does not paste the whole Mini App into chat.

## Safety

- New Pack code stays under `pack/`.
- `CCI_SOURCE` stays `mock`. `LIVE_CCI` stays false.
- Mission timelines render fixture events only.
- Hunt is a quality-tier badge on a Mission, not a separate object.
- Copy does not promise profit.
