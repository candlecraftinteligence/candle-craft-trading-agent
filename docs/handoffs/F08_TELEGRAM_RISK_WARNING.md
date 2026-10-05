# F08 — Explicit Telegram risk-warning text

## Phase

F08_TELEGRAM_RISK_WARNING

New public announcements that present an entry, stop, and target trade map now include one explicit risk line in the formatted text. This phase changes presentation and the alert-integrity audit of that text. It does not change admission gates, economic identity, lifecycle plan binding, recipient routing, reservations, delivery policy, cooldowns, retries, coalescing, or persisted historical content.

Application schema remains 26. Public quality remains 88 / grade A / RR 3. P5A owner monitoring and immutable progress, F03 population isolation, F04 replay semantics with capture disabled by default, F05 strict discovery and owned-client cleanup, and the accepted F06 readiness contract (`f06-runtime-release-readiness-v2`, tool `cci-runtime-checkpoint-readiness-v3`) are unchanged. `go_for_runtime_deployment` was not edited and stays false on F06 assessments.

No merge. No deployment. No historic resend. Independent acceptance is not claimed.

## Problem

On foundation main, `format_premium_public_signal_message` omitted an explicit risk warning and "not financial advice" from a confirmed trade-map announcement. `_message_has_risk_warning` treated the execution line "No chase" as warning evidence, including the message `No chase.` alone. `test_compact_signal_omits_disclaimer_but_preserves_internal_risk_warning` required that omission. The stored `TradeIdeaResult.risk_warning` did not show that a user received the warning.

## Implementation plan (executed)

1. Start from verified F06 merge `dea4f80174fa7e3e663c66da753ef9ebd13dc19b`. That merge has no file differences from accepted F06 head `401330526a8f3eaadfda2f5322a67f496ec5c221`.
2. Add one canonical public line, once, in the body before the signature:

   `Risk warning: Not financial advice. Trading can result in losses.`

3. Put that line on confirmed, watchlist, triggered, and watchlist-upgraded cards, and on research-watch announcements only when the stored geometry is a real trade map.
4. Leave prices, RR, invalidation, status labels, and the signature unchanged. A missing price stays `N/A`. The canonical line is not a substitute for invalidation or for a missing economic plan.
5. Audit emitted message parts. A warning counts only when a line starts with `Risk warning:` and the remainder is present and is not an execution-discipline placeholder. "No chase" and "manual execution" alone are not evidence.
6. Do not change event keys, outbox state, or stored payload text. Newly rendered announcements get the line. An already reserved or sent economic event keeps its persisted payload and is not announced again because the formatter changed.
7. Replace the omission test with assertions for the canonical line and for unchanged internal `risk_warning` metadata.

## Safety constraints

- No order execution, withdrawal, transfer, live scanner, listener, Telegram send, Runtime database access, or `.env` commit.
- DEV flags stay `LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`. `SOURCE_REPLAY_CAPTURE_ENABLED` was not enabled.
- Public admission remains quality 88, grade A, and RR 3.
- Schema remains 26. No migration, backfill, hash rewrite, or resend of pending or sent rows.
- Status labels stay honest. WATCH, STALKING, TRIGGERED, and research watch do not become CONFIRMED because a warning was added.
- The internal `risk_warning` field is still stored and is not copied into the public card.

## Before and after

Synthetic confirmed card. Prices are fixtures, not a live signal.

Before:

```text
🐺 ICPUSDT · LONG · SCALP/SWING

🟢 SIGNAL CONFIRMED

A · Score 91 · 3.22R

━━━━━━━━━━━━━

🎯 ENTRY 2.391 – 2.395

🛡 SL 2.3765

TP1 2.414

TP2 2.4282

TP3 2.44

━━━━━━━━━━━━━

🧠 EDGE

Sweep ✓ · BOS ✓ · OB/Fib ✓

⚔️ EXECUTION

Wait for the mapped zone. No chase.

🐺 Hunt live.

CCI · Signal. Structure. Execution.
```

After, the same values, with one added line before the signature:

```text
🐺 Hunt live.

Risk warning: Not financial advice. Trading can result in losses.

CCI · Signal. Structure. Execution.
```

"No chase" remains the execution instruction. It is not the risk warning.

## Message inventory

Covered. The warning is in the formatted body once, before the signature.

| Announcement | Formatter | Real caller |
| --- | --- | --- |
| Confirmed trade map | `format_premium_public_signal_message` | `format_telegram_signal_message(SIGNAL_CONFIRMED)`, `format_signal_confirmed_alert`, `format_trade_alert` for a non-rejected idea, `AlertAgent.format`, `format_valid_setup_message`, lifecycle delivery of `SIGNAL_CONFIRMED` |
| Watchlist trade map | `_format_public_signal_message(confirmed=False)` | `format_telegram_signal_message(WATCHLIST)`, `format_watchlist_alert`, `format_simple_public_signal_message`, `format_premium_watchlist_message`, lifecycle watchlist delivery |
| Triggered trade map | `_format_public_signal_message(triggered=True)` | `format_telegram_signal_message(SETUP_TRIGGERED)`, `format_triggered_setup_message` |
| Watchlist upgraded | `_format_public_signal_message(confirmed=True)` | `format_watchlist_upgraded_message`, `format_watch_activation_alert` |
| Research watch with a valid map | `format_research_watch_message` | `format_telegram_signal_message(RESEARCH_WATCH)`, `format_research_watch_alert`, `maybe_send_research_watch_alerts` |

A lifecycle confirmation that follows a watchlist still uses the premium confirmed card (`upgraded_from_watchlist` selects the routing type `WATCHLIST_CONFIRMED`). That card carries the warning and still says `SIGNAL CONFIRMED`. The separate upgraded card used by watch activation says `SIGNAL CONFIRMED · WATCHLIST UPGRADED` and also carries the warning.

The live public router still does not send a standalone TRIGGERED announcement. `deliver_for_symbol` records `public_triggered_internal_only`, and a same-batch confirmation coalesces it. The rendered triggered card carries the warning so the text is covered if a caller formats it. That suppression was not changed.

Research watch without a valid map still prints `Trade map:` and `N/A — waiting for clean confirmation.` It does not receive the warning, because there is no entry, stop, and target plan. It still does not say confirmed.

Excluded, with the reason:

| Text | Reason |
| --- | --- |
| `LIMIT_HIT` | Follow-up that the mapped zone was reached. It names the entry zone and a reaction instruction. It does not present a new stop and target plan. |
| `TP1_HIT`, `TP2_HIT`, `TP3_HIT` | Outcome-only target updates. |
| `SL_HIT`, `INVALIDATED`, `EXPIRED`, `NO_LONGER_TRACKING` | Outcome or status updates. Invalidated text may still say "No chase." That line is not warning evidence. |
| `format_public_no_trade_message` and rejected `format_trade_alert` | No trade map. Status stays `NO VALID SETUP`. |
| Menu, help, about, donate, wolf briefing, watchlist stage dashboard, command center | Administrative or summary text, not a new trade-map announcement. |
| `format_signal_detail` | On-demand detail screen opened from the menu. It shows a stored trade map, and it is not a new pushed announcement. It does not yet include the canonical line. See limits. |

## Audit

`build_alert_integrity_manifest` evaluates the joined `message_parts` when parts exist. The unsplit `formatted_message` is used only when no parts were supplied. A warning that remains only in `formatted_message` after the parts dropped it is `message_missing_risk_warning`.

A line is evidence only when it begins with `Risk warning:` and the remainder is present. Blank, `N/A`, `NA`, `NONE`, and `NULL` are not present. These exact remainders are execution discipline, not a warning: `no chase`, `no chase.`, `manual execution`, `manual execution.`, `manual execution only`, `manual execution only.`, `manual execution only. manage risk.`, `manage risk`, `manage risk.`

This is an exact label check, not a natural-language classifier. An older labeled line such as `Risk warning: This is not financial advice. Position size must be based on stop-loss risk, not desired profit.` still counts. `Not financial advice.` alone does not. `No chase.` alone does not.

`message_sha256` is still the SHA-256 of the supplied formatted text. Auditing a legacy body does not insert the canonical line and does not mint a new hash for that body. A legacy body without the label is a blocker, `message_missing_risk_warning`, which is the truthful classification.

`risk_warning_present` remains the internal trade-idea field check. `message_has_risk_warning` is the emitted-text check. They are separate. An internal warning does not satisfy the public line, and the public line does not rewrite the stored field.

## Legacy, pending, and sent content

Public event identity stays `{plan_id}|{event_type}` from the canonical economic plan. The rendered sentence is not part of that key.

Synthetic temporary-database proof:

- A new confirmed delivery includes the canonical line once.
- Replacing that row's persisted payload and hash with legacy text that has no warning, while leaving the event `SENT`, then delivering the same economic setup again, sends nothing. The stored payload, message hash, part payload, status, and delivery state stay on the legacy text.
- Setting that same event back to `PENDING` with the legacy payload and recovering it sends the persisted legacy text. Recovery does not render a new card.

Pending and sent artifacts are not edited by this patch. A restart uses the outbox payload already stored. There was no delivery-policy change.

## Unchanged contracts

- Schema 26.
- Public gates: `MIN_PUBLIC_SETUP_QUALITY_SCORE` 88, `MIN_PUBLIC_SIGNAL_GRADE` A, `PUBLIC_SIGNAL_MIN_RR` and `DEFAULT_CONFIRMED_MIN_RR` 3.
- Canonical economic identities, lifecycle plan binding, recipient routing, reservations, cooldowns, retries, and coalescing.
- F03 population isolation, F04 replay and disabled capture, F05 discovery and cancellation, F06 readiness contract and `go_for_runtime_deployment` false.
- No strategy change.

## Verification

Local DEV only. Temporary databases and `FakeSender`. No network send.

- F08 file: `tests/test_f08_telegram_risk_warning.py`, 10 passed.
- Focused formatter, alert-agent, integrity, and coalescing set, including that file: 100 passed.
- Related lifecycle delivery, triggered/confirmed routing, public funnel, watch mode, outbox recovery, and TP milestone tests: 300 passed.
- `python -m compileall -q app tests` completed before the full suite, exit 0.
- Full `python -m pytest -o addopts=`: 2910 passed, 1 existing Starlette/httpx deprecation warning, exit 0, 488.35s. That run is the final tree, including the last assertion cleanup.
- `git diff --check` exit 0.

The old test `test_compact_signal_omits_disclaimer_but_preserves_internal_risk_warning` was replaced by `test_public_signal_states_risk_warning_and_preserves_internal_risk_warning`. It now requires the canonical line once and requires the stored `risk_warning` to remain on the idea and out of the public card. `test_verbose_confirmed_facts_do_not_expand_compact_signal` no longer requires the compact card to fit in one 300-character part. The canonical line makes that card longer than 300 characters. The test now requires that 80 extra confirmed facts leave the card identical to the baseline, that every part stays within 300 characters, and that the warning still appears once.

No skip, xfail, or weakened gate assertion was added.

## Delivery

- Branch: `fix/f08-telegram-risk-warning`
- Base SHA: `dea4f80174fa7e3e663c66da753ef9ebd13dc19b`
- Accepted F06 head contained in that merge: `401330526a8f3eaadfda2f5322a67f496ec5c221`
- Implementation SHA: `acdfe56e179c22963928d6f13a9f20182b042a42`
- Verified handoff SHA: `5bb238a377d05fdb3815a14b9b9c0258ea9e5529`
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/131
- Schema version: remains 26
- Runtime database: not opened, copied, or modified

Verified CI for `fe23f141d5ed563f54145ce091df1fa6eb3d9728`:

- Run: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37331889788
- Attempt: 1
- Result: success
- Job: Python 3.11 tests, ID `111837239096`
- Log result: `2910 passed, 1 warning in 180.75s (0:03:00)`

Run `37331636620` on that SHA was cancelled by the workflow concurrency group when run `37331889788` was queued. It is not the result.

Verified CI for handoff tip `5bb238a377d05fdb3815a14b9b9c0258ea9e5529`:

- Run: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37332620088
- Attempt: 1
- Result: success
- Job: Python 3.11 tests, ID `111839268769`
- Log result: `2910 passed, 1 warning in 203.46s (0:03:23)`

The documentation commit that records these results is the branch tip after that commit. Its pull-request check is the CI for the exact final head. Do not treat `acdfe56e179c22963928d6f13a9f20182b042a42` or `fe23f141d5ed563f54145ce091df1fa6eb3d9728` alone as the candidate.

## Limits

- `format_signal_detail` still shows an on-demand trade map without the canonical line. It is not a pushed announcement. Adding the line there is a separate presentation change.
- Research watch with an invalid map does not get the warning.
- Standalone TRIGGERED text is formatted with the warning and is still not publicly sent.
- Outcome follow-ups do not get a blanket warning.
- Historical and pending payloads stay as stored. They can fail the new audit as `message_missing_risk_warning`. This phase does not rewrite them, rehash them, or resend them.
- The audit does not infer a warning from prose. Only the `Risk warning:` label with a non-placeholder remainder counts, including older labeled sentences.
- A Telegram part length below the warning line could split that line. Normal delivery uses the 4096-character splitter, which breaks on newlines first, and the warning is one short line before the signature.
- No production acceptance, merge, or Runtime rollout is claimed. Adam's separate code-only Runtime update remains outside this DEV phase and was not executed here.

## Operational actions

None on Runtime. No merge, no deployment, no listener, no watch loop, no Telegram send, no order, no live database access, and no historic resend.

DEV actions were this branch from current main, local tests, a draft pull request, and CI.
