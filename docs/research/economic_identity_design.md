# CCI P1 Economic Identity — Forensic Answers and Design

**Status:** design and test plan only. This phase does **not** implement, enable,
migrate, or replace economic identity. Dormant hygiene (`app/lifecycle/hygiene.py`,
`scripts/repair_lifecycle_hygiene.py`) remains offline repair, not a scan-path identity service.

`feature/lifecycle-plan-identity-hygiene-v1` is an ancestor of current `main`; hygiene
helpers are already on this checkout and are not activated by this document.

## P1 forensic answers

Each answer is `YES` / `NO` / `CONDITIONAL` / `NOT ESTABLISHED` from repository
inspection. A code path that permits a collision is not a measurement of historical frequency.

### 1. How is `setup_identity` generated?

**CONDITIONAL.** `app.lifecycle.identity.setup_geometry_identity` joins
`symbol|mode|direction|entry_low|entry_high|stop_loss|invalidation_reason`
with raw `str` values (missing → `N/A`). Wrapped by `state_machine._setup_identity`.
Recomputed from the current observation unless `current_state ∈ PLAN_LOCK_STATES`.

### 2. Can economically different plans produce the same `setup_identity`?

**YES.** `tp1`/`tp2`/`tp3` and RR are omitted. Distinct target ladders with the same
entry/stop/invalidation collide. Separate generations can also share the string.

### 3. Can one economic plan receive multiple `lifecycle_id` values?

**YES.** `generation_rotation_reason` starts a new id on `new_structural_anchor`,
`archived_lifecycle_new_setup`, or `completed_cooldown_new_setup`. Scalp vs swing
are separate current generations (`symbol, mode, direction`).

### 4. Can entry, stop, or TP values change while `lifecycle_id` remains constant?

**CONDITIONAL.** Frozen in `PLAN_LOCK_STATES` when stored values are not `N/A`.
**`TRIGGERED` is not locked.** `N/A` slots can be filled. Outcome evaluation uses
stored record geometry once persisted.

### 5. Can the same symbol and direction be reconciled incorrectly across scalp and swing?

**CONDITIONAL.** Lifecycle lookup is mode-partitioned. Public economic setup ids are
**mode-neutral** (`public_watchlist_dedupe_across_modes` defaults true). Treating a
public plan id as a lifecycle id can mis-attribute mode-specific lineage.

### 6. Are `N/A`, missing, or sentinel fields part of identity?

**YES.** Literal `N/A` participates in `setup_identity` and `plan_identity` digests.

### 7. Are rejection reasons part of identity?

**CONDITIONAL.** `failed_gate` is not a dedicated identity field. `invalidation_reason`
is included. Init can copy `failed_gate` into invalidation. `_fallback_signal_id`
hashes failed_gate/reason when no `lifecycle_id` exists.

### 8. Are raw stringified decimals used without tick normalization?

**CONDITIONAL.** `setup_identity` and lifecycle level strings: raw. Canonical
`plan_identity`: `Decimal.normalize()`, not exchange tick. Public watchlist/economic
ids: tick-quantized.

### 9. Are target revisions considered the same setup?

**CONDITIONAL.** Same `setup_identity` (TPs omitted). Different `plan_identity` (TPs
included) if unlocked geometry changes, producing another progress row for the same
`lifecycle_id`.

### 10. Is `structural_anchor` stable across scans?

**CONDITIONAL.** Non-`N/A` stored anchors are preserved; a changed observed anchor
rotates `lifecycle_id`. Unstable while `N/A` or before freeze. Producer:
`SetupLifecycleService._structural_anchor_from_symbol_result`.

### 11. Which identity is used for events, outcomes, public reconcile, TP/SL, duplicates?

**CONDITIONAL.**

| Operation | Identity |
| --- | --- |
| Lifecycle event append | `lifecycle_id` |
| Outcome progress | `(lifecycle_id, plan_identity)` |
| Outcome analytics | `lifecycle_id` + `final_outcome` |
| Public reconcile / follow-ups | `signal_id` / `public_watchlist_plan_id` / `event_key` plus economic match |
| Duplicate blocking | `UNIQUE(event_key)`, `UNIQUE(signal_id, alert_type)`, public plan hashes |
| Current generation | partial unique `(symbol, mode, direction) WHERE is_current=1` |

### 12. Where can ambiguity, collisions, or inconsistent lineage arise?

**YES (surfaces exist).** Shared `setup_identity` across TPs/generations; raw vs
normalized decimals; `N/A` tokens; mode-neutral public vs mode-split lifecycle;
`_fallback_signal_id` from current snapshot; UUID generations when anchor is `N/A`;
multi-row outcomes per lifecycle; `CONFIRMED`↔`ACTIONABLE_A_GRADE` oscillation
(lifecycle ownership, later phase).

### 13. Does any code reconstruct identity from the current symbol snapshot rather than the original frozen plan?

**YES.** Unlocked `setup_identity`; structural anchor from observation; fallback
public signal id; public plan builders from current message + `symbol_result`.
Outcome path prefers stored record + `compatible_plan_identities`.

### 14. Are historical `SENT` public events tied to immutable plan economics?

**CONDITIONAL.** SENT/outbox rows persist `canonical_plan_id`, zones, stop,
`structural_anchor`, `source_modes` at event time. Follow-up matching also uses
**current** lifecycle geometry and can yield `AMBIGUOUS` / `NO_MATCH`. Not a hard
immutability guarantee.

### 15. Does the existing schema contain enough information to construct `setup_id` and `plan_version_id` without breaking historical compatibility?

**CONDITIONAL.** Columns `setup_id` / `plan_version_id` are **NOT FOUND**. Existing
fields can *derive* compatible future ids for rows with complete stored geometry:
`lifecycle_id` + `setup_identity` + `structural_anchor` + stored prices. Historical
`N/A` anchors/prices must be classified **ambiguous legacy**, not synthesized
confidently. Dual canonical/legacy `plan_identity` already exists via
`compatible_plan_identities`. A future migration is a separately reviewed phase.

## Weakness register (do not fix now)

| Weakness | Producer / consumer | Research consequence | Tests now | Missing evidence | Future boundary |
| --- | --- | --- | --- | --- | --- |
| TPs omitted from `setup_identity` | `identity.py` / Telegram setup delivery | Distinct economics look like one setup | `tests/test_lifecycle.py` generation isolation | Historical collision frequency | P1 identity implementation |
| `plan_identity` includes `lifecycle_id` | `outcome_policy.py` | Cannot version economics independently of generation | outcome tests | — | New hash must drop lifecycle_id from economic content |
| `TRIGGERED` not in plan lock | `PLAN_LOCK_STATES` | Prices can move mid-generation | confirmed freeze tests; TRIGGERED gap | runtime frequency | Lifecycle ownership phase |
| Public mode-neutral ids | `telegram_lifecycle.py` | Scalp/swing collapse | public watchlist tests | mismatch rate vs modes | Keep public idempotency separate from setup_id |
| UUID when no anchor | `new_setup_generation_id` | Non-replay-deterministic | lifecycle tests | how often anchor is N/A | Require anchor or quarantine |
| Dual outcome tables | `outcomes.py` / service analytics | Join fan-out; not one trade | storage uniqueness tests | live fan-out rate | Authoritative outcome phase |
| `valid_activations` ≠ lifecycle fills | `watch_mode.py` vs `outcome_events.py` | False zero-activation world | watch vs lifecycle tests separate | live co-occurrence | Activation accounting phase |
| Hygiene offline only | `hygiene.py` | Invalid geometry can linger | `test_lifecycle_geometry_hygiene.py` | whether repair is used in runtime | Do not enable as silent rewrite |

## Critique of the six-id model

| Concept | Persist now? | Verdict |
| --- | --- | --- |
| `observation_id` | Derive `(run_id, symbol)` | No new table. Already unique. |
| `setup_id` | New, later | Yes, for lineage across generations. Do not reuse `setup_identity` string. |
| `plan_version_id` | New, later | Yes, immutable economics. Do not reuse `plan_identity` (it contains `lifecycle_id`). |
| `lifecycle_id` | Keep | Mutable state machine. Compatible as generation id. |
| `simulated_trade_id` | Later | Only after fill policy is explicit. Do not mint from TP progress. |
| `public_event_id` | Keep `event_key` | Delivery idempotency. Must not become economic identity. |

**Smallest coherent future design:** keep `lifecycle_id` and `event_key`; add
`setup_id` + `plan_version_id` as *new* hashes stored alongside existing fields
(prospective writes first). Do not replace public dedupe keys in the same change.
Do not add `observation_id` or `simulated_trade_id` until fill/outcome ownership
is specified.

## Economic-content fingerprint vs occurrence identity

An **economic-content fingerprint** is a hash of frozen prices and policy, independent
of when it was seen. Identical economics on 1 May and 1 June are the same *content*,
not the same *trade occurrence*.

**Occurrence identity** requires `setup_id` (lineage) + `plan_version_id` (frozen
content) + a fill/occurrence id (time and policy). Repeated observations of the same
current generation must not mint new setup ids. Genuine re-entry after a completed
generation (new structural anchor or completed cooldown) mints a new `lifecycle_id`
today; future code should attach that generation to the same `setup_id` when the
structural lineage is the same, and a new `plan_version_id` only when economics change.

## Proposed identity components

Include in **economic content** (`plan_version_id`) only if present at plan freeze:

| Component | Why | Authoritative source | If missing | Change effect |
| --- | --- | --- | --- | --- |
| Identity schema version | Prevent silent reinterpretation | constant e.g. `cci-plan-version-v1` | refuse | new schema version |
| Verified instrument id | Symbol string is not venue-safe | scanner symbol + exchange; CMC map is future/shadow | quarantine | new setup |
| Direction | Long/short economics | stored `direction` | refuse | new setup |
| Horizon / mode | Scalp vs swing are different plans | stored `mode` | refuse | new setup |
| Structural anchor | Lineage of the sweep/setup | stored `structural_anchor` | `setup_id` non-deterministic; quarantine | new setup_id if changed |
| Entry bounds | Geometry | stored `entry_low`/`entry_high` | refuse if required | new plan_version |
| Stop | Risk | stored `stop_loss` | refuse | new plan_version |
| TP1/TP2/TP3 | Payoff | stored targets | missing optional TP: explicit `null` vs unknown | new plan_version if present values change |
| Exit-policy version | How hits are evaluated | **NOT FOUND** today | mark unavailable; do not invent | new plan_version when policy exists |
| Strategy/config version | Challenger isolation | provenance hash is **not** this; separate strategy version **NOT FOUND** | unavailable | metadata unless it changes fill/exit rules |

Exclude from economic identity: rejection reasons, readiness, quality scores,
display buckets, transport message ids, Telegram ids, `valid_activations`,
provenance hashes, `event_key`.

`setup_id` should latch: instrument + direction + mode + structural anchor +
schema version. It must **not** include TPs (those version the plan) and must **not**
include `lifecycle_id`.

## Canonicalization

- Version the identity schema explicitly (`cci-plan-version-v1`).
- Canonical JSON or `\x1f`-joined fields with documented key order.
- Decimals: reject non-finite; do not use float. Tick-quantize using
  **verified instrument tick metadata** when present at freeze time.
- If tick metadata is missing historically: **ambiguous legacy**, do not silently
  round. Off-tick values: reject or quarantine; never silently snap two different
  prices onto one tick if that would merge distinct economics.
- Optional missing TP: encoded as typed `null` / `absent` in the version schema,
  distinct from unknown required economics (`N/A` today is unsafe).
- Do not use Python `hash()`.
- Keep a separate **economic-content fingerprint** (no occurrence time) for
  research clustering; do not use it as the trade id.

## Lineage, supersession, re-entry

- Repeated observations: same `lifecycle_id` while current; no new `setup_id`.
- Target revision while locked: should be impossible; if it happens, treat as
  defect, not a new confident version.
- Target revision while unlocked: new `plan_version_id`, same `setup_id` if anchor
  unchanged; supersession pointer (design), no historical rewrite.
- New structural anchor: new `lifecycle_id` today; future `setup_id` changes with
  anchor (new lineage).
- Completed cooldown / archive reactivation: new generation; lineage rule must be
  explicit in implementation tests.
- Do not invent confirmations or fills to glue occurrences together.

## Compatibility

- Keep writing current `setup_identity`, `lifecycle_id`, `plan_identity`, public
  `event_key` unchanged in the first identity implementation.
- Add parallel columns or JSON fields only in a **migrated** later phase (not authorized now).
- Public-event idempotency stays on `event_key` / plan reservation.
- Historical SENT messages remain referenced by stored `canonical_plan_id` +
  `message_hash`; do not rebuild from the latest symbol snapshot.
- Ambiguous legacy rows: quarantine classification, not synthetic confident ids.
  Quarantine is a design recommendation, not permission to rewrite history.
- Rollback: new ids must be ignorable by old readers.

## Affected modules (future implementation phase)

`app/lifecycle/identity.py`, `outcome_policy.py`, `state_machine.py`, `service.py`,
`repositories.py`, `outcomes.py`, `app/alerts/telegram_lifecycle.py`,
`app/alerts/public_identity.py`, `app/storage/database.py` (schema — requires
migration review), `app/storage/repositories.py`, tests under `tests/test_lifecycle.py`,
public delivery tests. **Do not start that work here.**

## Future test plan (next identity phase)

1. Same entry/stop, different TPs → different `plan_version_id`, same `setup_id` if anchor matches.
2. Same economics, different days, no fill → same content fingerprint, not the same trade occurrence.
3. Scalp vs swing same symbol/direction → different `setup_id` (mode in lineage).
4. Tick vs raw string (`100` vs `100.0`) → one plan_version after canonicalization; document tick policy.
5. Off-tick / non-finite → reject or quarantine, never silent merge.
6. Missing required economics → ambiguous legacy, not a synthetic id.
7. `lifecycle_id` rotation on new anchor → new generation; lineage assertions.
8. Public `event_key` unchanged by adding parallel ids.
9. Outcome fan-out still detected until outcome-ownership phase.
10. No strategy gate, RR, Telegram transport, or lifecycle transition changes in that PR either unless separately authorized.

**Stop at INSPECT → MAP → DESIGN → TEST PLAN.**
