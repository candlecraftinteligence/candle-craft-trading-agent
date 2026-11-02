# Telegram Precision Brief

## Purpose and implementation plan

Replace repetitive public signal copy with a compact, evidence-led presentation:
the observed sweep/structure, remaining entry condition, unchanged economic
levels, visible invalidation, optional source context, and useful native buttons.
Lifecycle updates describe only the new tracked event.

1. Add a deterministic presentation beside the existing legacy formatter.
2. Carry the chosen presentation through the existing durable outbox unchanged.
3. Send rich bodies with Telegram's `sendRichMessage`, preserving current
   preflight, provider-acknowledgment and retry classification.
4. Test formatting, escaping, missing evidence, market mapping, guard behavior,
   reservation, recovery, duplicate prevention and the existing suite.
5. Review in a feature-branch PR. Merge and runtime rollout remain owner actions.

## Activation and rollback

`TELEGRAM_SIGNAL_PRESENTATION=precision` selects the new format for newly
created public signal/lifecycle messages. The default is `legacy`, allowing a
controlled rollout and explicit comparison before changing public delivery.
Research-watch messages keep their existing format.

The new presentation uses Telegram Bot API rich-message support (10.1+) and
the supported rich HTML table, details, heading and button elements. Actual
font, spacing and colors depend on the subscriber's Telegram client/theme.
The design image is a concept, not proof of native-client rendering.

Switching back to `legacy` changes newly created intents only. Already reserved
intents keep their exact persisted presentation. Do not rewrite their payloads
or delivery history. A provider rejection remains a recorded failure: there is
no automatic plain-text resend after a rich request, especially on timeout or
ambiguous acceptance.

## Evidence and safety constraints

- No setup-selection, RR, confirmation, lifecycle-transition or eligibility
  logic changes. No executions, transfers or withdrawals.
- Only stored sweep and structural levels are narrated. A sweep flag does not
  establish a reclaim close; missing or contradictory detail is marked N/A or
  Unverified. No generic checklist is converted into invented confirmations.
- Canonical context supplies the plan. Conflicting entry-zone evidence is
  marked Unverified instead of narrating an overlap with a different plan.
- Invalidation, pending entry condition, target caution and risk warning remain
  visible. Only supporting context is collapsed.
- A zone touch does not prove an entry fill. TP/SL and breakeven refer to tracked
  setup events; actual fills and realized results remain Unverified.
- Rich HTML, buttons and chart link form one versioned, hashed, atomic outbox
  payload. Oversized rich bodies are rejected before splitting/transmission.
- The existing destination guard, reservation, dry-run and disabled-delivery
  policies still apply before network activity.
- No `.env` or live database access is required. Do not test by starting a second
  Telegram listener or scanner watch loop.

## TradingView button

The button links to the **perpetual** symbol on the stored exchange, for example:

`https://www.tradingview.com/chart/?symbol=BINANCE%3AQNTUSDT.P`

Only Binance and Bybit USDT perpetual mappings are supported here. Unknown
exchange/symbol mappings omit the chart action rather than silently selecting
another exchange or spot market. TradingView listing availability still needs
checking for an actual published symbol.

This is an HTTPS chart link with a web destination, not a guaranteed mobile-app
launcher. Telegram's in-app browser, the operating system, installed app version
and user link settings determine whether TradingView opens in its app. No
undocumented `tradingview://` scheme, device fingerprinting, redirect service or
third-party tracking is introduced.

Checked on 2026-11-02: TradingView's Android asset-links declaration associates
its domain with its Android app. Its iPhone `apple-app-site-association` explicitly
excludes `/chart/` URLs (including saved-chart paths) when a `symbol` query is
present. Therefore this exact-market link must not be advertised as a guaranteed
iPhone app launch. A generic app-opening link would lose the requested market.

Before public rollout, check from Telegram on iOS and Android, with and without
TradingView installed. Confirm the exchange, perpetual contract and usable web
fallback; record whether app handoff actually works. Do not label the button
“Open app” without that verification.

## Verification and Git workflow

Use the existing development Python environment, a feature branch and separate
test databases. Run `python -m pytest`; inspect `git diff --check` and the change
list to confirm no strategy/gate or live runtime changes. Commit and push the
feature branch, then open a PR against main; never merge automatically.

Automated checks mock the Telegram HTTP boundary. Native Telegram rendering
and TradingView phone handoff require the device checks above. No public test
message is sent by this phase.

### Verification record — 2026-11-02

- Base: `main` at `1d16314`. Branch: `feature/telegram-precision-brief`.
- Full `python -m pytest`: 4,603 passed, 2 failed, 1 existing dependency warning.
  The failures were a shifted source-line inventory anchor and the historical
  rollback commit missing from the fresh main-only clone.
- Preserved the pinned source positions by placing the new setting after the
  existing settings, and fetched the exact historical rollback commit. Neither
  failing test was weakened or removed.
- `python -m pytest --last-failed`: **2 passed** after those corrections.
- New feature tests plus both corrective rechecks: **46 passed** (44 new tests).
- `git diff --check`: clean. Strategy, scoring, setup geometry, confirmation
  gates, minimum RR and database schema files are unchanged.
- Native-client rendering and phone handoff are not yet verified; keep the PR
  in draft until the private-preview review. No production activation occurred.

References:

- https://core.telegram.org/bots/api#rich-messages
- https://core.telegram.org/bots/api#rich-message-formatting-options
- https://developer.android.com/training/app-links
- https://www.tradingview.com/.well-known/assetlinks.json
- https://www.tradingview.com/apple-app-site-association
