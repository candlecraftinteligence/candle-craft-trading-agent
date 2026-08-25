# CONFIRMED lifecycle and public-delivery contract

CCI intentionally keeps three concepts separate:

1. A **strategy-valid setup** passed scanner strategy gates and may become a trade idea.
2. A **lifecycle `CONFIRMED` transition** records lifecycle progression. In the outcome engine's entry-fill path, it means the stored entry zone was touched by a newly closed execution candle and the entry was simulated or confirmed.
3. A **public `SIGNAL_CONFIRMED` candidate** is a lifecycle `CONFIRMED` transition that independently passes the public delivery prefilter.

Lifecycle `CONFIRMED` is therefore necessary but not sufficient for public delivery.

## Same-scan entry-fill progression

The outcome engine can deliberately emit the ordered batch

`ACTIONABLE -> TRIGGERED -> CONFIRMED -> EXECUTING -> MANAGING`

from one newly closed execution candle when that candle touches the stored entry zone. `TRIGGERED` records the entry-zone touch. The later transitions use the reason `Entry fill simulated or confirmed.` This path does not wait for another confirmation candle and does not consume `confirmation_count`; those confirmation-cycle settings apply to the normal observation state machine, not this entry-fill progression.

This is an execution semantic and a naming mismatch with the phrase "final confirmation." It is not proof by itself that the setup qualifies for a public confirmed alert. The scanner-side public prefilter must still evaluate the projected `CONFIRMED` transition even when `TRIGGERED` was delivered earlier in the same batch.

## Public `SIGNAL_CONFIRMED` gates

The current hard gates are:

| Gate | Current contract | Relationship to scanner validity | Rejection point |
| --- | --- | --- | --- |
| Lifecycle state | Projected transition must be exactly `CONFIRMED` | Additional lifecycle requirement | Before transport |
| Public policy | Confirmed delivery must be enabled by the configured public signal policy | Delivery policy, not strategy validity | Before transport |
| Grade | Minimum `B+` | Intentionally separate from basic setup validity | Before transport |
| Setup quality state | `high_quality_trade` or `valid_but_lower_quality` | Public quality requirement | Before transport |
| Planned RR | Delivery context default is `3`; an authoritative per-strategy `effective_minimum_rr` replaces it. Production scanner configuration currently uses `2.5`. | Never lower than the authoritative strategy value; not a fixed independent `3` when strategy metadata is present | Before transport |
| Opportunity score | Scanner/service `min_score_for_idea`; normally `80` | Same configured idea threshold, defensively rechecked | Before transport |
| Technical score | Minimum `50` when a technical score is present | Public defensive minimum | Before transport |
| Trade plan | Trade idea present; direction, entry zone, stop, RR, invalidation and TP1-TP3 present | Public completeness requirement | Before transport |
| Plan integrity | Stored geometry, target order and target integrity must be valid | Hard safety gate | Before transport |
| Data health | Missing or unverified diagnostic data blocks confirmed publication | Public trust requirement | Before transport |
| Active rejection/failure | No current rejection reason, failed confirmation gate or invalidation conflict | Public trust requirement | Before transport |

There is no separate `SIGNAL_CONFIRMED` minimum score of `88`, technical minimum of `95`, opportunity minimum of `95`, strict Top-1 cap, hourly/daily cap, or same/opposite-side cooldown. Those constants and delivery limits belong to public watchlist policy. A previously sent `SETUP_TRIGGERED` alert deliberately does not deduplicate or suppress its generation's eligible `SIGNAL_CONFIRMED` event.

Target caution is not by itself a confirmed-alert rejection. Invalid target geometry or an active target-integrity failure remains blocking.

## Rejection audit

Every scanner-side public `SIGNAL_CONFIRMED` candidate that fails the confirmed prefilter is persisted in `telegram_alert_attempts` with:

- `attempted_alert_type = SIGNAL_CONFIRMED`;
- `telegram_status = blocked`;
- the exact combined prefilter reasons in `blocked_reason` and `error_message`;
- scan-run, RR, score, technical-score and trade-plan audit fields;
- a reason-hashed stored `alert_type` such as `SIGNAL_CONFIRMED_BLOCKED_<digest>`.

The reason-hashed stored type is intentional: a blocked audit record cannot consume the real `(signal_id, SIGNAL_CONFIRMED)` success identity. If the same generation later becomes eligible, it can create and send the real alert; repeated successful delivery remains deduplicated.

Rate-limit and cooldown reasons already emitted by public watchlist policy are normalized as `PUBLIC_RATE_LIMIT` / `RATE_LIMIT_GATE` and `PUBLIC_COOLDOWN` / `COOLDOWN_GATE` by the public-alert funnel. They are documented for research visibility and are not newly applied to `SIGNAL_CONFIRMED`.

## Metrics

`scan_runs.confirmed_setups` remains a final-state snapshot count for historical compatibility. A setup that transitions through `CONFIRMED` and ends the iteration in `MANAGING` is not counted by that field.

Watch/outbox summaries separately expose:

- `confirmed_transitions`;
- `public_confirmed_candidates`;
- `public_confirmed_prefilter_passed`;
- `public_confirmed_rejected_pretransport`;
- `public_confirmed_attempt_records`;
- `public_confirmed_sent`.

Blocked audit records are evidence of evaluation, not Telegram transport attempts. `public_confirmed_sent` is the transport-success measure.

No strategy-quality gate is weakened by this contract or its audit persistence.
