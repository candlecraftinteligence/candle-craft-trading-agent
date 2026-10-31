# Staging smoke — CCI The Pack

This procedure checks the mock Pack without a BotFather token and without a public HTTPS URL. Those two values are the only `EXTERNAL_BLOCKER` items. Leave them empty here. The API still boots. The bot process stays off.

`LIVE_CCI` stays `false`. `CCI_SOURCE` stays `mock`. Do not point `DATABASE_URL` at `scan_runs/main_live_runtime.sqlite` or any CCI Runtime database.

## What you need

- Docker Compose, or a local Postgres 16 role `pack` / database `pack` plus the API venv in `pack/apps/api`.
- No `PACK_TELEGRAM_BOT_TOKEN`.
- No real `PACK_WEBHOOK_SECRET`. The placeholder in `.env.example` is empty on purpose.

## Compose path

From `pack/`:

```bash
docker compose up --build
```

Postgres waits on `pg_isready`. The API command is `alembic upgrade head && uvicorn`. The image `CMD` does the same migration before uvicorn, so a single `docker run` of the API image also migrates. The web service waits until `GET /health` succeeds.

`docker compose --profile bot up` starts the Pack bot process. With an empty token it exits and does not take the API down. Do not set `BOT_REQUIRED=true` until `PACK_TELEGRAM_BOT_TOKEN` exists.

## Checks that do not need Telegram

Replace the host if you are not on localhost.

```bash
curl -s http://127.0.0.1:8000/health
```

Expect `live_cci` false and `cci_source` `mock`.

```bash
curl -s -X POST http://127.0.0.1:8000/api/auth/dev
```

Expect a session token when `DEV_BROWSER_MODE=true` and `PACK_ENV` is not `production`. Save it as `TOKEN`.

```bash
curl -s http://127.0.0.1:8000/api/missions -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://127.0.0.1:8000/api/missions/mock_setup_btc_h1_active/decision \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"decision":"NO_TRADE"}'
curl -s http://127.0.0.1:8000/api/quests -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8000/api/replay -H "Authorization: Bearer $TOKEN"
```

The replay list is concealed. The JSON must include the Replay disclaimer and must not include `TP_HIT` or a symbol such as `AVAXUSDT`.

```bash
curl -s -X POST http://127.0.0.1:8000/api/missions/mock_setup_avax_h1_tp/replay \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"chosen_tier":"HUNT","chosen_decision":"TRACK","evidence_reviewed":true,"idempotency_key":"smoke-1"}'
```

The reveal includes `AVAXUSDT`, `TP_HIT`, the teaching note, a score, and the same disclaimer. Repeat the same body. The second response has `created: false` and `xp_awarded: 0`.

Open `http://127.0.0.1:5173`. Home, Missions, Replay, and Profile load. Missions → MINE lists only locks stored for that session. Replay cards say “Concealed perp” until Reveal.

## Webhook stub

`POST /api/telegram/webhook` does not send Telegram messages and does not fail boot.

- No `PACK_WEBHOOK_SECRET`: response `503`.
- Secret set, header `X-Telegram-Bot-Api-Secret-Token` missing or wrong: `401`.
- Header matches: `{"ok": true, "handled": false}`.

Set the secret in Telegram with `setWebhook` only after the token and HTTPS URL exist. Until then leave the secret empty.

## EXTERNAL_BLOCKER

Live Telegram staging still needs:

1. `PACK_TELEGRAM_BOT_TOKEN` — a Pack BotFather token. Never the CCI production bot token.
2. `PACK_MINI_APP_URL` — a public `https://` origin for the Mini App.

Without those, mission push and “open the Mini App from Telegram” stay unproven. Everything else in this smoke test can pass.
