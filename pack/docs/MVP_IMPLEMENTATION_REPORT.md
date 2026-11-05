# CCI The Pack — MVP implementation report

Status: **NEAR_COMPLETE_AWAITING_TELEGRAM_SECRETS**

This is not `CCI_THE_PACK_MVP_COMPLETE`. Live Telegram push and a TLS Mini App host are not proven. Those two items need secrets this repository does not have. Everything else in the Phase 1 mock MVP is implemented under `pack/`.

```
LIVE_CCI_INTEGRATION = NOT IMPLEMENTED
CCI_RUNTIME_MODIFIED = FALSE
ORDER_EXECUTION = FALSE
STARS = NOT IMPLEMENTED
PHASE_2_STARTED = FALSE
```

## Git

Branch: `feature/the-pack-mvp`. Pull request: #125.

Phase 0 visual baseline: `04541c7`. Slice A persistence: `88aac70`. Slice B quests and bot scaffold: `3d8c3b7`. This report is committed with the Slice C closeout. The commit that contains this file is the branch tip. Read it with `git rev-parse HEAD` on `feature/the-pack-mvp`. PR #125 records that sha after push. A hash pasted into this file would not survive as the tip of the commit that adds the file.

`git diff origin/main...HEAD` is limited to `pack/`. CCI Runtime (`app/`, `scripts/run_telegram_bot.py`, `scan_runs`, root Alembic, the live runtime database) is untouched.

## Scope

CCI The Pack is a Telegram-native discipline layer beside Candle Craft Intelligence. The MVP is a four-tab Mini App (Home, Missions, Replay, Profile) plus a Pack bot scaffold. Decisions are exactly TRACK, I TOOK THIS, WATCH ONLY, and NO TRADE. Hunt is a quality badge on a mission, not a fifth decision.

In scope for this build:

- Mock fixtures only (`MockFixtureSource`, `CCI_SOURCE=mock`)
- Postgres models, Alembic, immutable decision locks, journals, XP ledger
- Telegram `initData` HMAC and a session JWT
- Quests, achievements, notification prefs, Pack oath
- Replay attempts persisted server-side, concealed until reveal, scored on the server
- Rate limits, webhook secret stub, staging compose

Out of scope, and not started:

- Live CCI, Stars, wallets, exchange APIs, order execution
- Screenshot file uploads
- A deployed TLS container
- Sending Telegram messages

## Architecture

The web app is React 18 and Vite. It talks to a FastAPI service. The API syncs mock fixtures into Postgres on request and serves missions from that store. XP, locks, journals, replay attempts, quests, and achievements are server-owned. The client cannot set an XP amount. `extra` JSON fields such as `pnl`, `leverage`, `xp`, and `telegram_user_id` are ignored.

`LIVE_CCI=true` raises at boot. Any `CCI_SOURCE` other than `mock` raises at boot. `BOT_REQUIRED=true` with an empty `PACK_TELEGRAM_BOT_TOKEN` raises at boot. An empty bot token with `BOT_REQUIRED` unset leaves the API up and the bot process disabled.

## Data model

Postgres tables: `users`, `missions`, `mission_lifecycle_events`, `user_mission_decisions`, `journals`, `xp_ledger`, `quests`, `quest_completions`, `achievements`, `user_achievements`, `process_marks`, `replay_challenges`, `replay_attempts`.

Uniqueness that the tests hit with a real `IntegrityError`: one user per Telegram id, one quest completion per user/quest/period, one achievement unlock per user. Decision locks, journals, process marks, and replay idempotency keys are also unique. A second insert does not grant XP again.

Replay challenges store a masked brief. The teaching note and the preferred training decision live on the challenge rubric, not in the public brief.

## API routes

| Method | Path | Role |
| --- | --- | --- |
| GET | `/health` | Mock source, `live_cci` false |
| GET | `/api/missions` | Fixture list, including outcomes for mission detail |
| GET | `/api/missions/{id}` | Lifecycle as stored. Unknown id is 404 |
| POST | `/api/auth/telegram` | HMAC `initData`, then a session JWT |
| POST | `/api/auth/dev` | Synthetic session. 404 outside dev |
| GET | `/api/me` | Ledger XP, rank, streak, locks, prefs. Does not settle quests |
| POST | `/api/missions/{id}/decision` | Immutable lock |
| GET/POST | `/api/missions/{id}/journal` | User-reported journal. No CCI outcome field |
| GET | `/api/replay` | Concealed tapes plus the Replay disclaimer |
| GET | `/api/missions/{id}/replay` | Concealed brief. 404 when the fixture is not a replay |
| POST | `/api/missions/{id}/replay` | Reveal, server score, ledger XP |
| GET | `/api/quests` | Board, then one-time quest XP |
| GET | `/api/achievements` | Unlocked codes, then one-time achievement XP |
| POST | `/api/me/notification-prefs` | Owned by the session user |
| POST | `/api/me/oath` | Pack oath timestamp |
| POST | `/api/missions/{id}/mark` | Evidence or review mark |
| POST | `/api/telegram/webhook` | Secret check only. Does not send messages |

Mission detail still returns `outcome_code` for a resolved fixture. That is the CCI Outcome block. Concealment is the Replay brief and the Replay list, not the mission page.

## Auth

`initData` is checked with the Telegram WebApp HMAC. The key is HMAC-SHA256 of the bot token under `WebAppData`. `auth_date` older than `AUTH_MAX_AGE_SECONDS` (default 86400) is rejected. A tampered `user` payload is rejected. The session is HS256, 12 hours, `Authorization: Bearer`. A forged secret and an `alg=none` token are 401. The body cannot choose another user's id.

`POST /api/auth/dev` exists only when `DEV_BROWSER_MODE` is true and neither `PACK_ENV` nor `APP_ENV` is `production`. The synthetic id is `900000001`.

HMAC uses `PACK_TELEGRAM_BOT_TOKEN` when set, otherwise `PACK_BOT_TOKEN`. Neither value is a CCI production token. `.env.example` holds empty placeholders. `.gitignore` ignores `.env` and `.env.*` except `.env.example`.

## XP

Lock XP: NO TRADE 25, I TOOK THIS 20, TRACK 15, WATCH ONLY 15. Journal 30 for any self-reported result, including LOSS. Replay completion 15, plus 25 when the server score is at least 80. Evidence mark 5. Review mark 20. Achievement amounts stay on the achievement rows. Streak bonuses are 10 / 20 / 40 at 3 / 7 / 14 process days.

Daily caps: decision 80, journal_review 100, replay 90, quest 140. After 70% of a cap, the next award in that category is halved, then clamped to the remaining room. At the cap, the award is 0 and the ledger row still records the idempotency key.

Replay attempts 1–3 pay full XP, 4–5 pay half, 6+ pay a quarter, before the daily cap. The same idempotency key returns the stored reveal with `xp_awarded` 0.

Quest and achievement settlement runs on `GET /api/quests`, `GET /api/achievements`, and `POST /api/me/oath`. It does not run on lock, journal, replay, or `GET /api/me`, so those responses do not mix quest XP into `pack_xp`.

Wolf Rank names follow the architecture thresholds from SCOUT through ELITE. Rank is a name. It does not unlock a wallet, a trade, or a cosmetic inventory.

## Replay

`GET /api/replay` and `GET /api/missions/{id}/replay` return `concealed: true`, the masked title and thesis, evidence, and the locked disclaimer. They do not return symbol, outcome, teaching note, preferred decision, or quality tier. After `POST`, the response includes those fields, the server score (40 quality / 40 decision alignment / 20 evidence), and the disclaimer again. A later GET of the brief stays concealed and sets `attempted: true`.

The web Replay tab reads `GET /api/replay`. Cards say “Concealed perp”. Reveal posts the drill and only then shows the server symbol and outcome. The disclaimer is on the lobby and on the drill.

Locked disclaimer: “Replay scores measure pattern recognition practice on closed historical setups. They do not predict future results. Past CCI setups do not guarantee future performance. The Pack never executes trades.”

## Anti-overtrading

See [ANTI_OVERTRADING_AUDIT.md](ANTI_OVERTRADING_AUDIT.md). No quest, achievement, or XP rule rewards more trades, PnL, leverage, size, or win rate. NO TRADE is worth more than I TOOK THIS. Journal XP is higher than the I TOOK THIS lock. Empty Home routes to Replay and quests. Notification defaults: new mission and lifecycle resolution on; quest, streak, and Replay nudge off.

## Telegram behavior

The Pack bot is `pack/apps/bot` and reads `PACK_TELEGRAM_BOT_TOKEN` only. Without a token it exits 0 and does not stop the API. `/start` copy and the Open The Pack button are built in code and tested without aiogram. A `startapp` mission id opens that mission. An unknown id is a 404 from `GET /api/missions/{id}` and the web not-found state.

`POST /api/telegram/webhook` rate-limits, returns 503 when `PACK_WEBHOOK_SECRET` is empty, returns 401 when `X-Telegram-Bot-Api-Secret-Token` does not match, and returns `{ok: true, handled: false}` when it does. It does not process updates.

## Security review

Rate limits, in memory per process: auth 30/minute per client host, decision lock 60/minute, journal 20/hour, replay submit 10/hour, replay reads 120/minute, quest board 60/minute, process marks 30/hour, webhook 60/minute. The 31st auth attempt in a minute is 429 even if `initData` is valid. These counters reset on process restart and are not shared across replicas.

Pydantic rejects a lock outside the four decisions, a replay tier outside HUNT|STANDARD, a training decision outside the drill set, and an empty replay idempotency key. Journal text fields are length-capped. Unknown JSON keys are dropped.

Checked in tests: concealed brief before reveal, double submit keeps one ledger row, replay cap clamps to 0, auth rate limit, forged JWT, webhook 503/401/200, `LIVE_CCI=true` refuses boot, unknown mission 404, prefs cannot change another user or XP, decision conflict is 409, journal of another user is hidden.

Not claimed: a production rate-limit store, a live webhook delivery test, or a secret scan of a future deploy environment. This tree has no committed bot token. `PACK_SESSION_SECRET=local-dev-only-not-a-secret` in `.env.example` is a local placeholder.

## Docker and staging

`pack/docker-compose.yml` healthchecks Postgres, the API (`GET /health`), and the web dev server. The API image command and the compose command both run `alembic upgrade head` before uvicorn. The bot service is a Compose profile and stays off unless requested. [STAGING.md](STAGING.md) is a smoke test that does not require the bot token.

`EXTERNAL_BLOCKER`:

- `PACK_TELEGRAM_BOT_TOKEN` — Pack BotFather token, never the CCI production bot token
- `PACK_MINI_APP_URL` — public `https://` origin

## Tests and build

Run on 2026-09-21 from this closeout, before the commit that adds this file:

- `python -m pytest` in `pack/apps/api`: 42 passed
- `npm run test` in `pack/apps/web`: 13 passed
- `npm run typecheck`: passed
- `npm run build`: passed (Vite production bundle)

Visual smoke on a 390×844 Chrome viewport against the local API and Vite dev server: Home, Mission Detail (decision panel `position: sticky`, all four decisions inside the viewport), and Profile Den signals. `prefers-reduced-motion: reduce` sets the training-badge animation to `none`. Screenshot uploads of user journals remain deferred. These UI captures are walkthrough artifacts, not files in git.

## Definition of Done

Ticks are only for items that are true in this tree. Unticked items are not done.

### Architecture §12 — anti-overtrading

- [x] Never reward more trades, higher leverage, larger size, or higher PnL
- [x] NO TRADE XP ≥ I TOOK THIS XP for locking (25 ≥ 20)
- [x] Journal XP (30) is greater than the I TOOK THIS lock (20). A separate review mark is 20, equal to that lock, and is not required to beat it
- [x] Category XP daily caps and diminishing returns
- [x] No profit multiplier and no leverage multiplier
- [x] No trade-count leaderboard
- [x] CCI outcome stays separate from the user self-report
- [x] Empty state routes to Replay and quests, and does not invent missions
- [x] Quests never require taking a trade
- [x] Achievements do not use PnL, trade-count, or leverage types
- [x] Wolf Rank is a name only
- [x] New-mission and lifecycle notices default on. Quest, streak, and Replay nudge default off and can be enabled
- [x] Copy does not promise profit. The tagline treats profit as the outcome and discipline as the game

### Architecture §25 — MVP acceptance

- [ ] Brand presents as CCI The Pack with the tagline visible in onboarding. The tagline is on Home. There is a Pack oath control, not an onboarding flow
- [ ] User can receive a Mission push and open Mission Detail in the Mini App. Deep-link routing is implemented. Live push is not, because the bot token and HTTPS URL are missing
- [x] Evidence is readable. All four decisions lock. A lock is immutable
- [x] NO TRADE grants at least as much XP as I TOOK THIS
- [x] Lifecycle updates come from fixture events. The client does not invent them
- [x] A resolved mission shows CCI Outcome apart from Journal
- [x] A journal awards more XP than a bare I TOOK THIS lock
- [x] Daily XP caps are enforced on the server
- [x] Quests appear on Home. None require taking a trade
- [x] Eighteen achievements ship (A01–A18). Banned reward types are absent
- [x] Replay completes conceal → reveal with the disclaimer on both steps
- [x] Wolf Rank progresses from SCOUT using the architecture thresholds. Nothing functional unlocks
- [x] Profile shows process stats, not a monetary PnL leaderboard
- [x] `initData` HMAC is validated. Spoofed Telegram ids and forged sessions are rejected
- [x] Bottom nav is exactly Home, Missions, Replay, Profile
- [x] Empty state routes to Replay and quests with the Pack-wait copy
- [ ] Motion is restrained for `prefers-reduced-motion` and `html[data-motion=off]`. Telegram `performance_class` is not read, so LOW devices are not given a separate degrade path
- [ ] Deployed on a single container with TLS. Compose and a migrating image exist. Nothing here is deployed with TLS
- [x] No Stars, wallets, exchange APIs, or CCI Runtime writes

## Known limitations and Phase 2

- Screenshot file upload is deferred.
- Rate limits are per process memory.
- The webhook does not handle updates.
- Dev browser auth must stay off in production.
- Replay training labels (TRACK, TAKE, WATCH, NO_TRADE) are not live locks.
- Phase 2 live CCI is not started.

## Deferred on purpose

Stars, wallets, exchange connectivity, order execution, live CCI reads, and autonomous trading are not in this build and must not be inferred from the mock MVP.
