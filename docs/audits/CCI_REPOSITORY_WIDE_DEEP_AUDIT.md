# CCI Repository-Wide Deep Forensic / Architecture Audit

| Field | Value |
| --- | --- |
| Audit date (UTC) | 2026-09-19 |
| Audited source SHA | `eef92b89f168bbb016715f3486b9f6bf2824f53f` (`main`, merge of PR #121) |
| Schema | Application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **25** (`app/storage/database.py`) |
| Scope | Investigation and written findings only. No application, strategy, migration, test, or runtime-config change. |
| Live Runtime DB | **Not accessed** (workspace safety rule). `scan_runs/` in this checkout contains only `.gitkeep`. |
| Pytest in this environment | **Not executed.** `pytest` was not installed in the audit VM. Contract claims below are from source and test *text*, not a fresh suite run. CI at this SHA was not re-run here. |
| Evidence labels | **FACT** = observed in tracked source/tests/docs. **INFERENCE** = architectural consequence of facts. **HYPOTHESIS** = plausible but unproven. **RECOMMENDATION** = next action; never a claim that the system already behaves that way. |

This is not a trading-bot review. CCI is assessed as a Binance USDT perpetual-futures market-structure intelligence system whose intended product is quality setups, lifecycle, persistence, replay, and honest expectancy — not always-in-market activity, not signal spam, and not fake confidence.

**Design INTENT (verified against code, not assumed correct):**

- Strategy family: `liquidity_grab_pullback` (`app/strategies/liquidity_grab_pullback.py`)
- Timeframes: HTF 2D / Bias 12H / Structure 2H / Confirmation 15m
- Economic floor: ~2.5R minimum, prefer 3R–5R
- Lifecycle INTENT: `REJECTED → WATCH → STALKING → TRIGGERED → CONFIRMED → EXECUTING → TP_HIT / SL_HIT / INVALIDATED / COOLDOWN`

**Classification legend for forensic contracts:** SOUND / PARTIAL / WEAK / UNBOUND / UNREACHABLE / CONTRADICTED.

---

## SECTION 1 — EXECUTIVE ASSESSMENT

**Maturity classification: PRE-ALPHA RESEARCH SYSTEM with an advanced-prototype operational scanner and gated Telegram delivery path.**

CCI is past a toy scanner. It has a deterministic strategy engine, a richer-than-documented lifecycle state machine, WAL SQLite persistence at schema v25, a deep public-delivery prefilter, and a layered forensic-contract stack (P0–P3, R0, EVAL_SEMANTICS, STORAGE_SINGLE_COPY, EVALUATION_POLICY_MANIFEST, SOURCE_EVIDENCE_BOUNDARY). That stack is unusually honest about what it cannot count.

It is **not** a system that can yet claim expectancy, unique trades, or exact-as-run decision replay. The research contracts themselves prove that:

- unique-trade count is **UNAVAILABLE** (`app/analytics/evidence_contract.py`);
- evaluation context `complete` is **hard-coded False** (`app/analytics/outcome_ownership.py`);
- runtime and replay evaluators are **non-equivalent by implemented policy** (`docs/research/evaluation_semantics_contract.md`, `tests/test_evaluation_semantics_contract.py`);
- evaluator inputs (`lifecycle_execution_candles`, `lifecycle_decision_timestamp`, batch delivery) are `exclude=True` on `ScannerSymbolResult` (`app/pipeline/scanner_runner.py`);
- evaluation-policy and source-evidence libraries are **unbound** to production evaluators and storage.

**What works (FACT):**

- Closed-candle eligibility via `closed_candles_as_of` (`app/data/candle_integrity.py`).
- Strategy sweep = wick-beyond-swing + close-back; BOS/CHoCH = body close beyond swing (`detect_liquidity_sweep`, `detect_structure_shift`).
- Authoritative minimum RR cannot be configured below 2.5 (`validate_configured_minimum_rr` in `app/core/minimum_rr.py`); CONFIRMED requires 3.0 (`CONFIRMED_MIN_RR` in `app/lifecycle/state_machine.py`); public confirmed/watchlist floors are A / 88 / 3R in delivery code.
- `ORDER_EXECUTION_ENABLED=true` fails Settings load (`app/core/config.py`).
- Telegram public send requires `LOCAL_MANUAL_MODE`, `TELEGRAM_SIGNALS_ENABLED`, and not dry-run (`TelegramSender.preflight`).
- Prospective CONFIRMED↔ACTIONABLE oscillation is repaired (`next_state_for_observation` CONFIRMED hold + `tests/test_lifecycle_ownership_repair_p2b.py`).
- Prospective scan-run payload dedup exists (`symbol_refs_v1`, schema v25).

**What does not work as a research system (FACT):**

- No admitted plan-evaluation episode (R0). Current keys cannot recover it.
- No durable candle possession or decision timestamp on stored scans.
- Research expectancy reads `replay_results`, not lifecycle outcome progress (`app/research/queries.py`).
- Dual outcome tables with different uniqueness (`UNIQUE(lifecycle_id, plan_identity)` vs `UNIQUE(lifecycle_id, final_outcome)`).
- PostgreSQL/Alembic/FastAPI stack is scaffold; scanner runtime is SQLite.

**Single largest threat:** treating replay rows, lifecycle generations, or public-delivery events as unique trades or as a trustworthy expectancy denominator.

**Single most important next phase:** observational binding of *what the evaluators actually saw* (policy ID + source/cutoff evidence + decision timestamp / candle possession) — still without computing expectancy, still without weakening gates, still without autonomous execution.

**STOP:** strategy/RR/confirmation changes, expectancy claims, CMC as setup authority, live Runtime DB mutation, order execution.

---

## SECTION 2 — ARCHITECTURE MAP

### 2.1 Dual stack (FACT)

| Stack | Path | Runtime role |
| --- | --- | --- |
| **Operational CCI** | `scripts/run_scan.py` → `app/pipeline/scanner_runner.py` → `LiquidityGrabEngine` → `SetupLifecycleService` → SQLite `app/storage` → `TelegramLifecycleDeliveryService` | **In use.** Schema v25. |
| **Scaffold API / Postgres** | `app/main.py` FastAPI `/health` only; `app/models/*`; `app/db/session.py`; `alembic/` with **empty** `alembic/versions/` (`.gitkeep` only) | **Not on the scanner path.** `get_db_session` / `get_engine` are defined only in `app/db/session.py` and are not imported elsewhere. |
| **Adjacent narrative tool** | `src/x_hype_prompt_agent/` | Isolated hype/prompt agent. `tests/test_safety.py` covers this package, not CCI scanner safety. |

**INFERENCE:** README still presents PostgreSQL + Alembic + FastAPI as the product database. Operators who migrate Postgres will not move scanner/lifecycle/Telegram state.

### 2.2 Producer → transformation → persistence → consumer (operational path)

```
Exchange public klines (BinanceFuturesClient.get_klines → /fapi/v1/klines)
  → normalizers (app/data/normalizers/binance.py)
  → closed_candles_as_of (app/data/candle_integrity.py)
  → in-memory CandleBatchDelivery (app/data/candle_batch_evidence.py)  [NOT persisted]
  → LiquidityGrabEngine.analyze (sweep 15m, BOS 15m, 2H informational, 2D synthetic)
  → RiskManagerAgent / OpportunityScoringEngine / TradeIdeaAgent / AlertAgent (dry-run artifact)
  → validate_setup_quality (app/analytics/setup_quality.py)
  → ScannerSymbolResult
        lifecycle_execution_candles / lifecycle_decision_timestamp / batch delivery = Field(exclude=True)
  → SetupLifecycleService.apply_to_run_result
        observation_from_symbol_result → evaluate_lifecycle_transition (latch P1 IDs)
        → evaluate_closed_candle_outcomes (if candles present)
  → store_scan_result (scan_runs + symbol_results + setup_candidates [+ replay_results if --replay]
        + symbol_health)
  → TelegramLifecycleDeliveryService.deliver_for_run
        eligibility → public gate → confirmed prefilter → outbox → TelegramSender.preflight
  → optional JSON sidecars: latest_scan.json, watch_state.json, performance_memory.json
```

**Public Telegram is not driven by `AlertAgent`.** Scanner still calls `AlertAgent.send` with `dry_run_alerts` for an older trade-idea artifact (`scanner_runner.py`). Canonical public delivery is `app/alerts/telegram_lifecycle.py`.

### 2.3 Identity namespaces (must not be mixed)

| Namespace | Symbol | Meaning |
| --- | --- | --- |
| Observation | `symbol_results` `(run_id, symbol)` | One scan of one instrument |
| Candidate | `setup_candidates` row | Geometry snapshot; **no UNIQUE(run_id, symbol, mode)** |
| Setup identity string | `setup_lifecycle_records.setup_identity` | Geometry string; **UNSAFE** for unique-trade counting (P0) |
| Lifecycle generation | `lifecycle_id` | Generation; unique current `(symbol, mode, direction)` via partial index |
| Economic plan (legacy) | `plan_identity` | Includes `lifecycle_id` — mixes generation into economics |
| P1 structural lineage | `setup_id` | Venue/symbol/direction/mode/structural_anchor |
| P1 immutable plan version | `plan_version_id` | setup_id + entry/stop/targets/invalidation; **no fill/exit policy** |
| Public plan | `canonical_plan_id` / `event_key` | Delivery identity, not a trade |
| Replay fingerprint | `replay_results.setup_fingerprint` | Different hash; not an episode |
| Admission | **none** | R0 future unit |

### 2.4 Module map (size is an architecture fact)

| Module | LOC (tracked) | Role |
| --- | --- | --- |
| `app/alerts/telegram_lifecycle.py` | 14847 | Public lifecycle delivery, gates, coalescing, outbox integration |
| `app/pipeline/scanner_runner.py` | 5234 | Scan orchestration, MTF fetch, result assembly |
| `scripts/run_scan.py` | 4953 | CLI / watch / persist / telegram / research entry |
| `app/strategies/liquidity_grab_pullback.py` | 2940 | Sole setup-generation engine |
| `app/research/queries.py` | 2697 | Read-only research CLI |
| `app/lifecycle/service.py` | 1812 | Observation mapping + service apply |
| `app/backtesting/strategy_replay.py` | 1678 | Bar-walk strategy replay |
| `app/lifecycle/state_machine.py` | 1651 | Transitions, CONFIRMED blockers, plan lock |
| `app/storage/database.py` | 1596 | Schema v25 + in-module migrations |
| `app/analytics/outcome_ownership.py` | ~1547 | P3A read-only projection |
| `app/analytics/evidence_contract.py` | 1486 | P0 dictionary |
| `tests/test_telegram_lifecycle_delivery_phase42.py` | 5060 | Public-delivery regression mass |

Tests: **136** modules, **2272** `test_*` functions (ripgrep count of tracked tests).

### 2.5 Intended vs implemented timeframes (preview; detail in §8)

| Layer | Default | Gates setups? |
| --- | --- | --- |
| HTF 2D | `htf_timeframe="2d"` (synthetic from 1D) | Context/trend only |
| Bias 12H | `bias_timeframe="12h"` | Trend context |
| Structure 2H | `DEFAULT_STRUCTURE_TIMEFRAME = "2h"` | **Informational only** (`_analyze_structure_layer`) |
| Execution 15m | `execution_timeframe="15m"` | Sweep detection |
| Confirmation 15m | `DEFAULT_CONFIRMATION_TIMEFRAME = "15m"` | BOS/CHoCH |

**FACT:** Default execution TF = confirmation TF = 15m. “Confirmation 15m” exists. “2H structure confirms the setup” does **not**.

---

## SECTION 3 — TOP CRITICAL RISKS

Each item: Severity, evidence, why it matters, failure mode, fix direction, **changes strategy YES/NO/POSSIBLY**.

### CRIT-1 — Runtime and replay evaluators are non-equivalent, and policy IDs are unbound

| | |
| --- | --- |
| **Severity** | **P0 research-correctness** (not a live-trading P0; there is no execution) |
| **Evidence** | `docs/research/evaluation_semantics_contract.md`; `tests/test_evaluation_semantics_contract.py`; `build_runtime_evaluation_policy` / `build_replay_evaluation_policy` in `app/research/evaluation_policy.py`; `inspect.getsource(evaluate_closed_candle_outcomes)` must not contain those builders (`tests/test_evaluation_policy_manifest.py`). Runtime entry: zone overlap `entry_touched` (`app/lifecycle/outcome_policy.py`). Replay entry: point containment `_simulate_trade` (`app/backtesting/strategy_replay.py`). Replay `_simulate_trade` receives the **full** execution series after causal detection (lookahead on the simulation series). Runtime targets skip the entry candle; replay can credit targets on the fill candle. Earlier TP then later stop: runtime keeps milestones and terminals `SL_HIT`; replay can return `TP1_HIT` with positive R. |
| **Why** | Any join of `setup_lifecycle_outcome_progress` to `replay_results`, or any “expectancy from replay equals live outcomes” claim, is methodologically invalid unless the comparison names the policy difference. |
| **Failure mode** | Published win-rate / R / “the scanner would have…” numbers that are actually a different entry model, a different horizon, and a different same-candle rule. |
| **Fix** | Keep both evaluators unchanged until a bound policy ID exists on each row. Do not “make them match” by silently changing runtime fill semantics. |
| **Changes strategy** | **NO** if the work is binding and disclosure. **POSSIBLY** if someone “aligns” replay to runtime by changing `_simulate_trade` and then treats historical replay rows as if they were always that policy. |

### CRIT-2 — Evaluator inputs and source evidence are not durable

| | |
| --- | --- |
| **Severity** | **P0 replay-readiness** |
| **Evidence** | `ScannerSymbolResult.lifecycle_execution_candles`, `lifecycle_decision_timestamp`, `lifecycle_execution_batch_delivery`, `lifecycle_batch_handoff` are `Field(exclude=True)` (`app/pipeline/scanner_runner.py` ~632–635). `CandleBatchDelivery` is in-memory only (`app/data/candle_batch_evidence.py`). `source_evidence.py` descriptors are always `BINDING_STATUS_UNBOUND`. Successor capture (`docs/research/prospective_batch_delivery_capture.md`) is excluded from `model_dump` / DB. P0: `source_receipt_time` NOT FOUND on the scan path; `decision_time` is not a receipt time (`app/analytics/evidence_contract.py`). |
| **Why** | Exact-as-run reconstruction of `evaluate_closed_candle_outcomes` requires the closed candle list and the decision timestamp used on that pass. Those objects die with the process. |
| **Failure mode** | “Replay the lifecycle” becomes “download klines later and hope they were the same series.” Historical reconstruction is labeled as such in SOURCE_EVIDENCE; it is not original possession. Empty `replay_results` is unavailable evidence, not 0% win rate (P0). |
| **Fix** | Observational persistence of possession/cutoff/policy — without promoting reconstruction to exact-as-run, without computing expectancy. |
| **Changes strategy** | **NO.** |

### CRIT-3 — No admitted observation unit; dual outcome stores; research queries use the wrong denominator

| | |
| --- | --- |
| **Severity** | **P0 expectancy-honesty** |
| **Evidence** | R0: admitted plan-evaluation episode is not persisted (`docs/research/observation_unit_contract_r0.md`, `tests/test_observation_unit_contract_r0.py`: existing keys cannot recover admission). Progress `UNIQUE(lifecycle_id, plan_identity)`; analytics `UNIQUE(lifecycle_id, final_outcome)` and **no plan column** (`app/storage/database.py`). P3A `canonical_outcome_per_plan_version.established: False`; `feeds_operational_decisions: False`. `app/research/queries.py` `_replay_stats` / `replay_expectancy` read `replay_results`; lifecycle funnel queries do not read `setup_lifecycle_outcome_progress`. P0 unique-trade count unavailable. |
| **Why** | Summing `scan_runs.confirmed_setups`, `DISTINCT lifecycle_id`, public `event_key`s, or filled replay rows manufactures a trade count the contracts forbid. |
| **Failure mode** | Operator or research report publishes expectancy; later forensic work shows the count mixed generations, deliveries, and simulations. Trust in CCI as an intelligence product collapses. |
| **Fix** | Keep P0 labels. Do not add a DISTINCT workaround. Next honest work is admission + policy + source binding (P5/P6 prerequisites), then reconciliation — not a new metric dashboard. |
| **Changes strategy** | **NO.** |

### Additional high risks (not the top three, still material)

| ID | Severity | Summary | Changes strategy |
| --- | --- | --- | --- |
| CRIT-4 | P1 ops | `telegram_lifecycle.py` is 14.8k LOC. Review risk, merge conflict, accidental gate edit. | NO if refactored without behavior change; POSSIBLY if “simplified” |
| CRIT-5 | P1 contract-drift | `docs/confirmed_public_delivery_contract.md` says confirmed min grade **B+**; code/tests use **A** / score **88** (`MIN_PUBLIC_SIGNAL_GRADE` in `app/analytics/public_signal_quality.py`). Hygiene doc still says schema **v16**. R0/EVAL_SEMANTICS docs still say schema **24**. | NO |
| CRIT-6 | P1 config | `Settings.validate_public_watchlist_min_rr` allows `>= 0` (`app/core/config.py`), while module constants floor public watchlist at 3.0. Three public/watchlist RR floors exist: state machine 2.5, eligibility 3.0, CONFIRMED 3.0. | NO if docs/constants aligned; YES if someone “fixes” by lowering code floors |
| CRIT-7 | P2 product | `a_grade_watch_candidate = False` hardcoded in `observation_from_symbol_result` (`app/lifecycle/service.py:540`). `A_GRADE_WATCH` is largely unreachable from live scanner observations. | NO (state exists; live producer does not set the flag) |
| CRIT-8 | P2 structure | 2H layer is informational; default 15m/15m collapses MTF confirmation. Opportunity scoring hard floor is **2.0R** (`MIN_RISK_REWARD_RATIO`) below strategy 2.5R. | POSSIBLY if 2H were promoted to a gate without evidence |
| CRIT-9 | P2 storage | `symbol_results.raw_result_json` still stores a full copy; JSON sidecars are a second store; `setup_candidates` / `replay_results` lack business-key UNIQUE. Live DB size **cannot be verified here**. | NO |
| CRIT-10 | P2 safety | `AlertAgent._send_telegram` and raw `send_telegram_messages` do not share `TelegramSender.preflight`. Production public path uses the sender; a future caller wiring AlertAgent live would bypass lifecycle gates. | NO |

---

## SECTION 4 — FORENSIC CONTRACT REVIEW

Classifications are for the **implemented contract as scoped in its own docs/tests**, not for the aspirational research unit.

| Contract | Paths | Key symbols | Class | Remaining gap |
| --- | --- | --- | --- | --- |
| **P0 Evidence Contract** | `app/analytics/evidence_contract.py`, `docs/research/evidence_contract.md`, `tests/test_evidence_contract.py` | `build_evidence_contract`, `CONTRACT_VERSION` (`cci-evidence-contract-v2`) | **SOUND** as a dictionary | Does not enforce writers. `valid_activations` still exists and is labeled UNSAFE. |
| **P1 Immutable Economic Plan Identity** | `app/lifecycle/economic_identity.py`, `docs/research/economic_identity_design.md`, `tests/test_economic_identity.py` | `mint_setup_id`, `mint_plan_version_id`, `latch_economic_identities` | **PARTIAL** | Latched on lifecycle write. No exit/fill policy in the hash (deliberate). Consumers (research, Telegram, analytics uniqueness) still largely use `lifecycle_id` / `setup_identity` / `plan_identity`. |
| **P2A Activation Accounting** | `app/analytics/activation_accounting.py`, P0 activation entity, `tests/test_activation_accounting_truth.py` | `project_activation_accounting`, `ACTIVATION_ACCOUNTING_VERSION` | **SOUND** (scoped) | Read-only. Not unique fills. Legacy `scan_runs.valid_activations` DEFAULT 0 on non-watch runs (`app/storage/database.py`, repositories persist path). |
| **P2B Lifecycle Ownership + oscillation repair** | `app/lifecycle/state_machine.py` CONFIRMED branch ~723–748, `tests/test_lifecycle_ownership_repair_p2b.py` | `evaluate_lifecycle_transition`, `next_state_for_observation` | **SOUND** prospective; **PARTIAL** historical | Historical rows not rewritten. Not an independent tracker. Other bidirectional edges remain (STALKING↔WATCHLISTED). |
| **P3A Outcome Ownership** | `app/analytics/outcome_ownership.py`, `docs/research/outcome_ownership_p3a.md`, `tests/test_outcome_ownership_contract.py` | `project_outcome_ownership` | **PARTIAL** | Diagnostic only. `feeds_operational_decisions: False`. Canonical owner not established. Direct-writer fan-out → `ambiguous_evaluation_context`. |
| **P3B1 Prospective plan attribution** | `app/lifecycle/repositories.py` `upsert_outcome_progress`, `docs/research/outcome_plan_attribution_p3b1.md`, `tests/test_outcome_plan_attribution_p3b1.py` | INSERT `plan_version_id`; ON CONFLICT does not update it | **PARTIAL** | First INSERT only. No legacy NULL backfill. Unlock/invariant paths withhold attribution. |
| **P3B2A Evaluation Context** | `outcome_ownership._describe_evaluation_context`, `docs/research/evaluation_context_p3b2a.md`, `tests/test_evaluation_context_p3b2a.py` | `complete: False` hardcoded | **SOUND** (honest boundary); **PARTIAL** (causal completeness) | Last-pass cutoff ≠ contiguous coverage. |
| **P3B2B Durable Eligibility Cutoff** | `app/lifecycle/outcomes.py`, schema columns `last_eligibility_decision_at`, `tests/test_durable_eligibility_cutoff_p3b2b.py` | `_with_applied_eligibility_cutoff` | **SOUND** as last-pass witness; **PARTIAL** vs original clock | Cannot recover original decision clock from ordinary dumps. |
| **P3_PREFIX** | `app/lifecycle/prefix_disposition_evidence.py`, `docs/research/durable_prefix_disposition_p3_prefix.md` | W/P/C envelope on last qualifying application | **PARTIAL** | Not whole-interval coverage. Malformed envelopes retained. |
| **R0_OBSERVATION_UNIT** | `docs/research/observation_unit_contract_r0.md`, `tests/test_observation_unit_contract_r0.py` | admitted plan-evaluation episode (future) | **PARTIAL** (spec + producer proofs) | **No admission persistence.** Doc still says schema 24 in places; code is 25 (**CONTRADICTED documentation**, not evaluator logic). |
| **EVAL_SEMANTICS** | `docs/research/evaluation_semantics_contract.md`, `tests/test_evaluation_semantics_contract.py` | paired `evaluate_closed_candle_outcomes` vs `_simulate_trade` | **SOUND** as a non-equivalence proof | Does not bind policy; does not authorize replay as runtime authority. Schema-24 wording is stale vs v25. |
| **STORAGE_SINGLE_COPY** | `app/storage/scan_payloads.py`, `docs/research/storage_single_copy.md`, v25 `raw_payload_format`, `tests/test_storage_single_copy.py` | `encode_scan_raw_payload`, `load_logical_scan_payload` | **PARTIAL** | Prospective `symbol_refs_v1` when byte-identical and smaller. Full copy remains in `symbol_results.raw_result_json`. Historical `inline_v1` unchanged. R0 matrix R (“STILL OPEN”) is **superseded for new runs**. |
| **EVALUATION_POLICY_MANIFEST** | `app/research/evaluation_policy.py`, `docs/research/evaluation_policy_manifest.md`, `tests/test_evaluation_policy_manifest.py` | `build_runtime_evaluation_policy`, `build_replay_evaluation_policy` | **UNBOUND** | Hashed manifests exist. Not imported by either evaluator. Not persisted on rows. Equal policy ID ≠ same episode/source. |
| **SOURCE_EVIDENCE_BOUNDARY** | `app/research/source_evidence.py`, `docs/research/source_evidence_boundary.md`, `tests/test_source_evidence_boundary.py` | `build_source_evidence_descriptor`, `BINDING_STATUS_UNBOUND` | **UNBOUND** (library); **PARTIAL** (in-memory capture) | Valid descriptor is always unbound. Resampling labeled (`DELIVERY_TRANSFORMED_RESAMPLED`) but not durable. |

**None of these contracts are UNREACHABLE in tests.** Production *binding* is the gap for policy and source.

### Historical-issue re-verification

| Historical issue | Status | Evidence |
| --- | --- | --- |
| Scan-result duplication | **PARTIALLY FIXED** | v25 `symbol_refs_v1` for `scan_runs.raw_payload_json` when eligible. `symbol_results` still full JSON. JSON sidecars remain. |
| Lifecycle oscillation (CONFIRMED↔ACTIONABLE) | **FIXED** (prospective) | P2B hold when `_confirmed_observation_ready`. Tests in `test_lifecycle_ownership_repair_p2b.py` and `test_confirmed_actionable_oscillation_is_no_longer_produced`. No historical rewrite. |
| Stale active setups | **PARTIALLY FIXED** (rules exist); **CANNOT VERIFY** live | Confidence decay; CONFIRMED exempt from decay-to-EXPIRED (`CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES`). Research stale query is a filter, not auto-invalidation (`DEFAULT_LIFECYCLE_STALE_HOURS` in `queries.py`). Live DB not inspected. |
| Zero / unreliable activation accounting | **PARTIALLY FIXED** | P2A separates watch alerts vs `ENTRY_ACTIVATED` events. Legacy field still present and UNSAFE. |
| Target-integrity warnings | **FIXED** as fatal vs soft split | Scanner/lifecycle/Telegram tests distinguish fatal blocks vs caution. P0 still marks overlapping warning metrics UNSAFE if used as counts. |
| RR mismatch | **FIXED** (authoritative wiring) | `minimum_rr.py` + strategy `rr_below_minimum` + CONFIRMED 3.0 + public 3.0. Residual: opportunity floor 2.0; Settings public watchlist RR can be configured to 0 (module constants still floor delivery). Prior audit CCI-P1-007 RESOLVED remains directionally true. |
| Empty replay | **STILL PRESENT** (semantic) | P0: empty `replay_results` = unavailable, not 0%. Persist only with `--replay` + `--store-scan`. This checkout has no replay rows. |
| Empty performance-memory | **REPLACED BY DIFFERENT RISK** | File store is optional; annotates only; must not override gates (`MEMORY_SAFETY_NOTE`). Risk is using empty/low-n memory as if calibrated. |
| Reconciliation issues | **STILL PRESENT** (episode-level); **PARTIALLY FIXED** (Telegram watchlist/outbox) | P3A diagnostic; R0 P5 still open. Telegram reservation/UNCERTAIN paths are heavily tested. |
| Missing source provenance | **PARTIALLY FIXED** | In-memory batch delivery + handoff association (PR #120). Omitted from persistence. Descriptor library unbound. |
| Logical cutoff vs receipt time | **STILL PRESENT** | P0 `source_receipt_time` not on scan path. P3B2B is last applied eligibility input, not receipt. |
| Resampling provenance | **PARTIALLY FIXED** | Source contract labels synthetic 2D resample. Not stored on scan rows. |
| Insufficient evaluator input serialization | **STILL PRESENT** | `exclude=True` fields. EVAL_SEMANTICS / SOURCE docs. |

Prior runtime-migration audit (`docs/audits/CCI_RUNTIME_MIGRATION_READINESS_REAUDIT.md`) resolved WAL init and v14→v16 preservation at **schema 16**. Current schema is **25**. Those WAL/safety patterns remain in `database.py`; the v16-era finding IDs are not a current schema inventory.

---

## SECTION 5 — DATA INTEGRITY REVIEW

### 5.1 Candle integrity (FACT)

`validate_candle_sequence` and `closed_candles_as_of` reject missing timestamps, duplicates, out-of-order, continuity gaps, and insufficient closed history (`CandleIntegrityReason` in `app/data/candle_integrity.py`). Eligibility is `close_timestamp <= decision_timestamp`. Scanner analysis and replay prefixes use this primitive.

**INFERENCE:** Open/future candles cannot enter the analysis window if callers actually use `closed_candles_as_of`. This is the intended fix for historical CCI-P1-001. This audit did not re-run that pytest group.

### 5.2 Missing vs zero vs unverified (FACT)

AGENTS.md and P0 agree: missing is N/A or UNAVAILABLE, not zero. Empty replay is unavailable. `unique_trade_count` is UNAVAILABLE with an explicit reason forbidding DISTINCT-on-lifecycle_id. Public setup quality has **no fallback** to opportunity/readiness/technical scores (`canonical_public_setup_quality_score`).

### 5.3 Geometry integrity (FACT)

Invalid stored geometry blocks monitoring transitions (`stored_plan_geometry_failure`, `INVALID_STORED_PLAN_GEOMETRY` in `outcome_policy.py` / `state_machine.py`). Hygiene quarantine is explicit, path-required, confirm-token apply (`app/lifecycle/hygiene.py`, `scripts/repair_lifecycle_hygiene.py`, `docs/LIFECYCLE_GEOMETRY_QUARANTINE.md`).

**CONTRADICTED docs:** quarantine runbook still says it “verifies schema v16”; `hygiene.py` requires current `SCHEMA_VERSION` (25).

### 5.4 Target integrity (FACT)

Fatal target-order / integrity gates vs soft `target_inside_chop` caution. Public confirmed path: caution is not by itself a rejection; invalid geometry remains blocking (`docs/confirmed_public_delivery_contract.md` + lifecycle defensive blockers). Strategy-evidence review (2026-08-15) correctly refused to weaken chop rejection without empirical outcomes — **still the right posture**.

### 5.5 Identity collision / fan-out (FACT)

- Current lifecycle row: partial unique index `ux_lifecycle_records_current_symbol_mode_direction` WHERE `is_current = 1`.
- Progress: `UNIQUE(lifecycle_id, plan_identity)` — physical owner, not an episode.
- Analytics: `UNIQUE(lifecycle_id, final_outcome)` — can fan out vs progress; no `plan_version_id`.
- Public events: `UNIQUE(event_key)`; sent/active unique indexes on `public_watchlist_event_key`.
- `setup_candidates` and `replay_results`: surrogate `id` only.

P1 `plan_version_id` uses Decimal canonicalization **without tick quantization** because tick metadata is not on the lifecycle write path (`economic_identity.py` module docstring). Off-tick values can be rejected if a caller passes `tick_size`; they are never silently rounded.

### 5.6 Evidence time (FACT)

Processing time (`evaluated_at`, event `timestamp`) is not candle-close occurrence time (P2A `PROCESSING_TIME_WATERMARK`). Cache TTL / capture clock / decision timestamp / receipt time are distinct; SOURCE_EVIDENCE forbids conflating them. This audit **cannot** verify whether production Runtime fetches populate capture clocks correctly; the capture is excluded from persistence regardless.

---

## SECTION 6 — DATABASE / STORAGE REVIEW

### 6.1 System of record (FACT)

Default path: `scan_runs/candle_craft.db` (`DEFAULT_DATABASE_PATH`). Writable profile: WAL, `synchronous=FULL`, `foreign_keys=ON`, busy timeout 5000 ms, autocheckpoint 1000 pages, bounded WAL init retry. WAL refusal or failed verification raises `StorageError` — no silent fallback to DELETE journal (`database.py`).

Migrations are **in-module** (`initialize_database`, `_ensure_column`, `_migrate_*` through v25), not Alembic. Alembic is configured for the unused Postgres models.

### 6.2 Tables and constraints (FACT)

| Table | Uniqueness / FK | Role |
| --- | --- | --- |
| `scan_runs` | PK `run_id` | Run snapshot + `raw_payload_json` / `raw_payload_format` + unsafe aggregate counters |
| `symbol_results` | `UNIQUE(run_id, symbol)` FK cascade | Full `raw_result_json` (the remaining copy) |
| `setup_candidates` | none beyond `id` | Flattened candidates; multiple rows possible |
| `replay_results` | none beyond `id` | Opt-in replay trades |
| `setup_lifecycle_records` | PK `lifecycle_id`; partial unique current (symbol, mode, direction) | Lifecycle SoT |
| `setup_lifecycle_events` | FK to records | Transition log |
| `setup_lifecycle_outcome_progress` | `UNIQUE(lifecycle_id, plan_identity)` | Closed-candle projection |
| `setup_outcome_analytics` | `UNIQUE(lifecycle_id, final_outcome)` | Second outcome store; **UNSAFE** to join naively |
| `telegram_alert_attempts` | `UNIQUE(signal_id, alert_type)` + public event uniques | Delivery audit including blocked hashed types |
| `public_alert_events` / `delivery_parts` | `UNIQUE(event_key)`; parts unique per index | Outbox |
| `symbol_health` / `symbol_health_events` | PK symbol; append-only-ish events | Adaptive priority, not expectancy |

P3B2B/P3_PREFIX columns on progress: `last_eligibility_decision_at`, `last_eligibility_prefix_evidence_json` (nullable). P1 columns on records: `setup_id`, `plan_version_id`, `economic_identity_reason` (nullable).

**Absent by design:** `admission_id`, `evaluation_episode_id`, `evaluation_policy_id`, durable `source_namespace`, candle OHLC series, `decision_timestamp`.

### 6.3 STORAGE_SINGLE_COPY (FACT)

`encode_scan_raw_payload` may emit `symbol_refs_v1` (`results[]` → `{symbol, sha256}`) when child JSON is byte-identical and compact form is smaller. Otherwise full `inline_v1`. `load_logical_scan_payload` reconstructs losslessly or raises `ScanPayloadIntegrityError` — no empty fallback. Rollback: `SCAN_RAW_PAYLOAD_INLINE_ONLY` / `inline_raw_payload=True`. Downgrade of a refs-written DB to a pre-v25 reader is **unsupported**.

### 6.4 Run provenance (FACT)

`app/storage/run_provenance.py` embeds git snapshot, allowlisted config hash, writer schema version in `scan_runs.runtime_stats_json`. It is **not** a link to lifecycle progress, evaluation policy, or candle possession.

### 6.5 JSON sidecars (FACT)

`scan_output.json`, `scan_runs/latest_scan.json`, `watch_state.json` (explicitly deprecated; DB is SoT — `WatchState.deprecation_note` in `app/watch_mode.py`), `performance_memory.json`, manifests. Gitignored. Checkpoint readiness treats them as external mutable state (`app/storage/runtime_checkpoint_readiness.py`). Dual persistence without a single checkpoint transaction.

### 6.6 Postgres (FACT)

SQLAlchemy models exist for candles, trades, ideas, alerts, journal, backtest runs. **No Alembic revisions.** Session helpers unused by scanner. `DATABASE_URL` in `.env.example` points at Postgres — a trap for operators.

### 6.7 Live size (FACT of scope)

R0 cited historical ~81.21 GiB / ~3.65 GB/day as **historical audit estimates, not current measurements**. This audit did not inspect Runtime PC `scan_runs/main_live_runtime.sqlite`. Capacity claims would be fabricated.

---

## SECTION 7 — LIFECYCLE REVIEW

### 7.1 Implemented state set vs INTENT (FACT)

Documented: `REJECTED → WATCH → STALKING → TRIGGERED → CONFIRMED → EXECUTING → TP_HIT / SL_HIT / INVALIDATED / COOLDOWN`.

Implemented (`SetupLifecycleState` in `app/lifecycle/models.py`): additionally `DISCOVERED`, `WATCHLISTED` (not `WATCH`), `ACTIONABLE_A_GRADE`, `A_GRADE_WATCH`, `MANAGING`, `EXPIRED`, `COOLED_DOWN`, `NO_LONGER_TRACKING`, `REMOVED`, `CANCELLED`, `CANCELED`, `ARCHIVED`.

**INFERENCE:** Product language “WATCH” maps to `WATCHLISTED` plus A-grade watch states. Funnel queries that only list the documented names **omit** A-grade states (`LIFECYCLE_FUNNEL_STATES` in `app/research/queries.py` has no `ACTIONABLE_A_GRADE` / `A_GRADE_WATCH`).

### 7.2 Ownership (FACT)

| Actor | Mutates lifecycle DB? |
| --- | --- |
| Scanner | No (produces `ScannerSymbolResult`) |
| `SetupLifecycleService` | Yes — `evaluate_lifecycle_transition` + `upsert_record` |
| `evaluate_closed_candle_outcomes` | Yes — progress, events, terminals |
| `outcome_events.advance_to_managing` | Yes — may chain up to four transitions on entry evidence |
| `hygiene.py` | Yes — explicit audited quarantine only |
| Telegram | **No** — consumes transitions |
| `--disable-lifecycle` | Skips service apply (`scripts/run_scan.py`) |

Latch of P1 identities happens inside `evaluate_lifecycle_transition` via `latch_economic_identities`. Generation rotation nullifies the current row when structural anchor changes (`app/lifecycle/identity.py` + service). `PLAN_LOCK_STATES` freeze geometry; **`TRIGGERED` is excluded** (plan remains unlocked).

### 7.3 Transition graph (FACT)

`ALLOWED_TRANSITIONS` is bidirectional among several early states (STALKING↔WATCHLISTED, TRIGGERED↔STALKING). CONFIRMED may go to ACTIONABLE_A_GRADE, A_GRADE_WATCH, EXECUTING, INVALIDATED, EXPIRED — not directly back to WATCHLISTED.

Dynamic target: `observed_state` → confirmation-cycle gate → `next_state_for_observation` → `transition_record`.

**P2B CONFIRMED hold (FACT):** if already CONFIRMED and A-grade candidate unfilled, stay CONFIRMED when `_confirmed_observation_ready`; otherwise demote to ACTIONABLE_A_GRADE. This prevents actionability-only ping-pong and also avoids falling through to `valid_trade_idea → EXECUTING` for an unfilled A-grade observation.

**Residual oscillation (INFERENCE):** STALKING↔WATCHLISTED on sweep loss is allowed by design. That is not the historical CONFIRMED/ACTIONABLE bug.

### 7.4 CONFIRMED blockers (FACT)

`_confirmed_observation_blockers` requires: not a rejected core status, no failed confirmation gate, no active rejection/invalidation, data health, valid trade idea, side, entry zone, stop, invalidation text, RR ≥ **3.0**, quality at least B+ *for lifecycle CONFIRMED*, setup quality in `{high_quality_trade, valid_but_lower_quality}`, technical ≥ 50 (observation default), opportunity ≥ idea threshold (default 80). Near-miss quality state cannot confirm.

**FACT — public confirmed is stricter on grade:** delivery uses `MIN_PUBLIC_SIGNAL_GRADE = "A"` and score 88. Lifecycle CONFIRMED can exist below public grade A (B+ allowed in `MIN_CONFIRMATION_GRADES`). Contract doc: lifecycle CONFIRMED is necessary but not sufficient for `SIGNAL_CONFIRMED`.

### 7.5 Entry-fill batch vs “confirmation” (FACT)

`docs/confirmed_public_delivery_contract.md`: outcome engine may emit `ACTIONABLE → TRIGGERED → CONFIRMED → EXECUTING → MANAGING` from **one** closed candle that touches the zone. `TRIGGERED` = zone touch; later reasons = “Entry fill simulated or confirmed.” This path does not consume `confirmation_count`.

When `lifecycle_execution_candles` is present, `observation_from_symbol_result` sets `entry_filled=False` and `closed_candle_outcomes_managed=True`, delegating fill to outcomes (`service.py` ~622–628).

**HYPOTHESIS:** Fast chaining can outrun the human narrative of STALKING/TRIGGERED if operators read CONFIRMED as “multi-scan confirmation.” The public prefilter is supposed to still evaluate the projected CONFIRMED even if TRIGGERED was already delivered in the same batch.

### 7.6 Wick vs close, reclaim, invalidation ownership (FACT)

Lifecycle **does not recompute** sweep/BOS. It maps scanner diagnostics (`sweep_detected`, `structure_shift_detected`, pullback failure, acceptance_status). `_structural_acceptance_invalidated` maps `TOO_DEEP`, `body_acceptance_failure`, `structural_breakdown`, selected failed gates.

Outcomes: entry = **range overlap** with zone (wick-inclusive); stop/targets = wick touch on closed ranges; entry activation causality = post-boundary **closed** execution candles (`outcomes.py`, `outcome_policy.py`).

**INFERENCE:** A setup can remain non-INVALIDATED in lifecycle if the strategy failed a gate not in that mapping.

### 7.7 Stale / cooldown / decay (FACT)

Default cooldown 24h from terminal (`DEFAULT_COOLDOWN_HOURS`). Confidence decay can EXPIRE decayable states except CONFIRMED (limit-entry waiting is expected). Hygiene quarantines malformed geometry; it does not sweep “old but valid” CONFIRMED plans.

**CANNOT VERIFY:** whether the live Runtime DB currently holds stale CONFIRMED rows.

### 7.8 A_GRADE_WATCH reachability (FACT)

`a_grade_watch_candidate = False` is assigned unconditionally in `observation_from_symbol_result`. The state remains in the enum, `ALLOWED_TRANSITIONS`, and CONFIRMED demotion branch. Live scanner observations should not newly *enter* A_GRADE_WATCH via that flag.

**INFERENCE:** Dead or test-only product surface unless some other writer sets the flag (none found on the scanner observation path).

---

## SECTION 8 — SCANNER / STRATEGY IMPLEMENTATION REVIEW

Classify: **bug** vs **questionable assumption** vs **unproven hypothesis**. Do not weaken gates to “fix” activity.

### 8.1 Engine (FACT)

Sole setup generator: `LiquidityGrabEngine` in `app/strategies/liquidity_grab_pullback.py`. Agents (`TradeIdeaAgent`, `RiskManagerAgent`, `TechnicalStructureAgent`, `DerivativesOrderflowAgent`, `AlertAgent`, `JournalAgent`) are pipeline stages/formatters, not a second signal engine.

Sweep (`detect_liquidity_sweep`): wick beyond confirmed swing ≥ `SWEEP_ATR_MULTIPLIER` (0.35 ATR) **and close back**. Frozen by `tests/test_phase40_guardrails.py`.

Shift (`detect_structure_shift`): **close** beyond prior LTF swing; CHoCH vs BOS from prior trend.

Pullback/reclaim/acceptance: `app/analytics/pullback_zones.py` / pullback intelligence; `body_acceptance_failure` is a hard quality/final gate.

Invalidation text is produced by strategy `_invalidation` and copied into lifecycle observation.

### 8.2 Timeframe implementation vs INTENT

| Intent | Implementation | Class |
| --- | --- | --- |
| HTF 2D | Synthetic resample from 1D; `htf_2d_trend` context | **Questionable assumption** if operators believe 2D is a venue 2D kline. SOURCE labels `TRANSFORMATION_SYNTHETIC_2D_RESAMPLE`. |
| Bias 12H | Fetched; trend context | FACT implemented as context |
| Structure 2H | Analyzed when `structure_analysis_required=True` (scanner sets True ~3521) | **Questionable assumption** relative to “2H confirms.” Docstring: informational only; qualification remains execution-sweep + confirmation-shift. |
| Confirmation 15m | BOS/CHoCH on confirmation candles | FACT. Default same series as execution 15m. |
| Execution 15m | Sweep | FACT |

**Bug (product, not crash):** default MTF confirmation is same-series 15m/15m. Changing `--confirmation-timeframe` is possible; it is not the default.

**Unproven hypothesis:** promoting 2H structure to a hard gate would improve expectancy. No empirical cohort in-repo (2026-08-15 strategy evidence review still holds: `replay_reports/` and `replay_validation/` are placeholders).

### 8.3 RR layers (FACT)

| Layer | Floor | Type |
| --- | --- | --- |
| `validate_configured_minimum_rr` | 2.5 cannot be lowered | Hard config |
| Strategy swing/scalp | 2.5; challenge 3.0 | Gate `rr_below_minimum` |
| Lifecycle CONFIRMED | 3.0 | Hard |
| Lifecycle initial public watchlist (`state_machine.PUBLIC_WATCHLIST_MIN_RR`) | 2.5 | Hard for that helper |
| `eligibility.PUBLIC_WATCHLIST_MIN_RR` | 3 | Hard |
| Public delivery constants | 3.0; target-caution 2.8 | Hard in module |
| `Settings.public_watchlist_min_rr` validator | **≥ 0** | **Config can be set below module floor** |
| Opportunity scoring | **2.0** | Hard filter inside scorer |
| Prefer 3–5R | Scoring tier `_risk_reward_score` | Preference only |

**INFERENCE:** IDEA path is still safe if strategy validity is required before scoring (`scanner_runner` order). A future path that scores without strategy validity could emit 2.0–2.49R ideas.

**Not a recommendation to raise opportunity floor in this audit’s code** — that would be a strategy-adjacent change and is out of scope. It is a documented inconsistency.

### 8.4 Quality, regime, portfolio, near-miss (FACT)

- `validate_setup_quality` / `FINAL_QUALITY_GATES` / `SetupQualityState` including `WATCHLIST_NEAR_MISS` (blocked from CONFIRMED).
- Regime optional (`market_regime_enabled` default False on `ScannerRunConfig`; run_scan may enable). Rejection appends `REJECTED_BY_REGIME` without wiping prior status history (reliability audit 2026-08-15; still the intended lineage).
- Portfolio selection and symbol health annotate/rank; climate risk multiplier is **informational only** (`scanner_runner.py` warning text).
- Performance memory annotates; docstring and tests: does not override scanner gates.

**Unproven:** regime compatibility as directional edge. Research docs mark regime/CMC as deferred; no 75%+ accuracy gate exists (correct).

### 8.5 Universe (FACT / prior P2 still open)

`app/universe/symbol_universe.py` uses public volume/market-cap style selection. Prior CCI-P2-009 (missing exchangeInfo contract status/tick/step/notional) was **OPEN** in the 2026-07-18 migration re-audit. This pass found no exchangeInfo contract-metadata gate in universe code. **STILL PRESENT** as a listing-risk (delisted/wrong-contract symbols), not as a strategy-gate weaken.

### 8.6 Adversarial notes (not exploit recipes)

- `--disable-lifecycle` produces ideas without lifecycle RR/CONFIRMED gates (scanner-only).
- Aggressive toggle and confirmation-cycle settings can change how often CONFIRMED is reached; they are operator-controlled, not hidden.
- Same-candle conservative stop-wins is runtime policy in code, not a versioned row binding — historical rows cannot prove they used it.

**No recommendation to add execution, reduce RR, or auto-promote WATCH to CONFIRMED.**

---

## SECTION 9 — REPLAY READINESS

**Verdict: PARTIAL. Strategy bar-walk replay is implemented. Exact-as-run lifecycle/decision replay is not. Replay is not runtime outcome authority.**

| Capability | Ready? | Evidence |
| --- | --- | --- |
| Re-run `LiquidityGrabEngine` on closed prefixes | **Yes (engine)** | `StrategyReplayEngine` + `closed_candles_as_of`; causality tests exist (`tests/test_replay_causality_phase2.py`, `tests/test_strategy_replay.py`) |
| Persist replay trades | **Conditional** | Only `--replay` and `--store-scan`; else `_replay_result_records` empty |
| Reconstruct runtime `evaluate_closed_candle_outcomes` from SQLite | **No** | Excluded candles/timestamps; no OHLC on progress |
| JSON lifecycle “replay” audits | **Observability only** | Field/status checks (`app/analytics/lifecycle_replay_audit.py`); no re-eval |
| Align replay outcomes to runtime outcomes | **No** without policy bridge | EVAL_SEMANTICS matrix |
| Empty table interpretation | **Honest if P0 followed** | Unavailable ≠ 0% |
| Tracked empirical candle cohort | **No** | `replay_reports/.gitkeep`, `replay_validation/.gitkeep`; 2026-08-15 review: 0 empirical rows |

**Lookahead (FACT):** detection uses a closed prefix; `_simulate_trade` then walks the **full** execution series (EVAL_SEMANTICS). That is disclosed non-equivalence, not a hidden HTF-open-candle leak of the CCI-P1-002 kind. Historical CCI-P1-002 was marked RESOLVED for HTF closed-as-of in the migration re-audit; this audit did not re-run that pytest group.

**INFERENCE:** Bar-walk re-discovery every bar is not the same experiment as “one production scan at end of window.” Survivorship/selection bias if used as production mimicry.

**One-liner:** Replay can simulate the strategy on historical klines; it cannot yet prove what CCI decided on a given Runtime pass.

---

## SECTION 10 — EXPECTANCY READINESS

**Verdict: NOT READY for authoritative expectancy. READY only for disclosed, sample-warned replay-trade arithmetic on opt-in `replay_results`, which is a different object than a CCI plan episode.**

| Use case | Verdict | Evidence |
| --- | --- | --- |
| CLI `replay_expectancy` | Arithmetic exists; warns if n < 30 | `MIN_RELIABLE_SAMPLE_SIZE = 30`, `_replay_stats` |
| Performance memory `average_r` | File buckets from `ingest_replay_summary` | Must not override gates |
| Lifecycle-progress expectancy | **Not implemented** | `queries.py` does not query outcome progress/analytics for expectancy |
| Unique-trade expectancy | **Unavailable** | P0 `unique_trade_count` |
| Public-signal hit rate as expectancy | **Invalid** | Delivery events ≠ trades; blocked audits are not sends |
| Policy-stamped historical claims | **Not ready** | Policy IDs not on rows |
| 75%+ win-rate product claim | **Not justified** | Explicitly rejected in research contracts |

P0: TP1/TP2 are milestones; `TP_HIT` without proved TP3 is not TP3; TP then SL retains both facts at runtime and must not be collapsed to a winning replay TP1.

**One-liner:** Expectancy cannot be honestly claimed until an admitted episode, bound policy, bound source/cutoff, and non-fan-out outcome owner exist — none of which are jointly enforced today.

---

## SECTION 11 — TEST COVERAGE GAPS

**FACT of inventory:** 136 test modules, 2272 test functions. CI: Python 3.11, `compileall`, full `pytest`, safe Telegram/execution env (`.github/workflows/ci.yml`). No coverage gate, no Postgres service, 20-minute timeout.

### Heavy (FACT)

Telegram lifecycle (`test_telegram_lifecycle_delivery_phase42.py` ~131 tests / 5060 LOC), `test_run_scan.py`, `test_scanner_runner.py`, lifecycle/outcomes, storage migrations through v25, public funnel/quality, forensic contract suites listed in §4, WAL concurrency tests, RR invariance (`test_authoritative_minimum_rr.py`, `test_phase40_guardrails.py`).

### Gaps (FACT / INFERENCE)

| Gap | Why it matters |
| --- | --- |
| This VM did not run pytest | Audit cannot refresh the 2026-08-15 “full suite pass” claim at SHA `eef92b8` |
| Postgres/Alembic unused — almost no integration tests | Fine if stack stays dead; dangerous if someone “enables” it |
| FastAPI = health only | No API contract tests for scanner |
| `A_GRADE_WATCH` live producer | Tests can construct observations; scanner hardcodes False |
| No empirical replay cohort tests | Fixtures are synthetic by design; they do not prove market edge |
| Research queries vs outcome tables | Little/no test that expectancy is *forbidden* from progress joins (P0 tests cover unique-trade unavailability; query layer can still print `_replay_stats` zeros) |
| `AlertAgent` live send vs `TelegramSender` | Divergence not gated as a single preflight |
| Live DB / WAL sidecar behavior on Runtime PC | Out of scope here |
| `tests/test_safety.py` name | Covers x_hype, not CCI |

**FACT:** Funnel analytics must not send or write (`tests/test_public_alert_funnel_safety.py`). Regime/portfolio penalty invariance tests exist.

---

## SECTION 12 — TECHNICAL DEBT

| ID | Severity | Item | Evidence |
| --- | --- | --- | --- |
| TD-01 | High | `telegram_lifecycle.py` 14.8k LOC | Maintenance / accidental gate edit |
| TD-02 | High | Doc/code drift: confirmed grade B+ vs A; schema 16/24 vs 25; R0 storage matrix R stale vs v25 | Cited docs vs code |
| TD-03 | High | Unbound policy/source presented as “phases complete” if misread | Module headers correctly say unused; README phase laundry list invites overclaim |
| TD-04 | Medium | Dual persistence SQLite + JSON sidecars | `watch_mode.py`, checkpoint readiness |
| TD-05 | Medium | Unused Postgres/Alembic/FastAPI | `alembic/versions/.gitkeep`; unused session |
| TD-06 | Medium | Multiple RR/public floors | `minimum_rr.py`, state_machine, eligibility, Settings, scoring |
| TD-07 | Medium | `run_scan.py` ~5k LOC god-script | CLI + watch + persist + telegram |
| TD-08 | Medium | `setup_candidates` / `replay_results` lack business UNIQUE | `database.py` DDL |
| TD-09 | Medium | P1 consumers not migrated | R0 matrix B |
| TD-10 | Medium | `LOCAL_MANUAL_MODE` not validated at Settings load | Only `TelegramSender` |
| TD-11 | Low | README “Phase 1…46C” narrative vs forensic P0–R0 stack | Onboarding confusion |
| TD-12 | Low | `x_hype` `test_safety.py` name | Misleading |
| TD-13 | Low | No lockfile | `requirements.txt` ranges only |
| TD-14 | Info | FastAPI version `0.1.0` health-only | `app/main.py` |
| TD-15 | Info | CMC not implemented | Workspace rules vs zero CMC client |

---

## SECTION 13 — PERFORMANCE / SCALE

**FACT:** Default CLI `--universe-size 50` (`scripts/run_scan.py`). Microstructure/liquidation/order-book optional, default **off**, caps 100 symbols (`Settings`). WebSocket bounds tested (`tests/test_websocket_transport_bounds.py`). Process RSS sampling exists (reliability audit / process memory). SQLite WAL autocheckpoint 1000 pages.

**INFERENCE:** Full-universe watch + nested JSON copies + 14.8k Python delivery module + optional order-book bootstrap is the scale risk, not FastAPI. STORAGE_SINGLE_COPY reduces *run payload* duplication for new runs only; per-symbol JSON and lifecycle/telegram tables still grow with time.

**CANNOT VERIFY:** current Runtime DB size, RSS, or scan duration.

**HYPOTHESIS:** without archival (`app/storage/maintenance.py` exists), a long-lived Runtime DB will again become an operational constraint. Historical 81 GiB figure is not current evidence.

---

## SECTION 14 — WHAT SHOULD NOT BE CHANGED YET

1. **Strategy gates:** sweep 0.35 ATR + close-back; BOS close; `BASE_MIN_RR` 2.5; challenge 3.0; `body_acceptance_failure` / target-order integrity; chop target rejection.
2. **`validate_configured_minimum_rr` cannot go below 2.5.**
3. **CONFIRMED blockers and P2B CONFIRMED hold.**
4. **Public confirmed prefilter** and `canonical_public_setup_quality_*` (no fallback scores).
5. **Separation:** strategy-valid ≠ lifecycle CONFIRMED ≠ public `SIGNAL_CONFIRMED`.
6. **`ORDER_EXECUTION_ENABLED` must remain false**; no withdrawals/transfers.
7. **TelegramSender preflight order:** manual mode → signals enabled → dry-run → credentials → routing.
8. **Outbox reservation / hashed blocked `SIGNAL_CONFIRMED` identities** (blocked rows must not consume success identity).
9. **P0 labels** (UNAVAILABLE vs zero; unique-trade forbidden).
10. **P3A `complete: False` and `feeds_operational_decisions: False`.**
11. **EVAL_SEMANTICS non-equivalence** — do not silently retcon replay rows.
12. **WAL verification fail-closed.**
13. **Dev PC constraints:** dry-run, no full-time watch listener, no live Runtime DB mutation.
14. **CMC:** remains deferred; must not create/approve setups.
15. **Performance memory:** annotation only.

Changing any of the above to “get more signals” or “make replay match” would violate CCI core principles.

---

## SECTION 15 — WHAT SHOULD BE REMOVED OR SIMPLIFIED

Removal/simplification is **not authorized by this audit as a code change**. Candidates for a later, explicitly scoped cleanup:

| Candidate | Why | Risk if done carelessly |
| --- | --- | --- |
| Unused Postgres models + empty Alembic + health-only FastAPI | False SoT | Someone might “replace SQLite” without a migration design |
| Dead `setup_only` policy branches | Config maps `setup_only` → `lifecycle` | Public policy regression |
| `A_GRADE_WATCH` live path or the hardcoded False | Unreachable producer | Accidental new public state |
| `AlertAgent` live Telegram send | Bypasses lifecycle sender | If deleted, keep dry-run console artifact |
| Root `IMPLEMENTATION_*.md` / overlapping audits | Navigation | Historical evidence must remain; index rather than delete blindly |
| JSON `watch_state.json` writes | Deprecated | Break compatibility scripts |
| Duplicate public threshold constants | Three sources of truth | **Do not unify by taking the minimum** |

**Do not simplify** by collapsing runtime and replay evaluators, by DISTINCT-counting lifecycle IDs, or by shrinking `telegram_lifecycle.py` through weaker gates.

---

## SECTION 16 — RECOMMENDED NEXT DEVELOPMENT PHASES

Order is dependency-shaped. None compute expectancy. None enable execution. None weaken gates.

### Phase N1 — Durable evaluation input & source witness (observational)

| | |
| --- | --- |
| **Problem** | Decision timestamp, execution candles, and batch delivery die with the process (`exclude=True`). |
| **Why now** | Blocks exact-as-run replay (P6) and honest reconstruction. Contracts already describe the hole. |
| **Scope** | Persist a bounded, explicit witness: decision timestamp, timeframe, closed-window hashes or candle possession proof, capture vs processing clocks, resampling label. Fail closed on integrity. |
| **Out of scope** | Admission table, expectancy, changing `entry_touched`, CMC, Telegram copy. |
| **Evidence required** | Round-trip tests: in-memory batch → DB → load equals hashes; missing witness ≠ invent candles. |
| **Areas** | `scanner_runner.py` fields, `candle_batch_evidence.py`, `source_evidence.py`, schema migrate v26+, repositories |
| **Invariants** | Reconstruction labeled reconstruction; original possession distinct; no gate change |
| **Tests** | Extend `test_prospective_batch_delivery_capture.py`, `test_source_evidence_boundary.py` |
| **Research benefit** | Makes P6 possible later |
| **Risk** | DB growth — pair with STORAGE_SINGLE_COPY lessons; do not store unbounded duplicate payloads without hashing |
| **Depends on** | Existing SOURCE_EVIDENCE + batch capture (done, unbound) |

### Phase N2 — Bind evaluation-policy IDs to progress and replay rows (observational)

| | |
| --- | --- |
| **Problem** | Manifests exist but evaluators do not record which policy produced a row. |
| **Why now** | EVAL_SEMANTICS already proved non-equivalence; unmarked rows cannot be compared. |
| **Scope** | Write `evaluation_policy_id` (content hash from existing builders) onto new progress and new replay rows. Do not rewrite legacy. Do not change rules. |
| **Out of scope** | Making runtime and replay identical; setting `complete=true` |
| **Evidence** | Tests that a runtime progress row’s policy_id matches `build_runtime_evaluation_policy`; replay row matches replay builder; missing ID stays NULL/UNAVAILABLE |
| **Areas** | `evaluation_policy.py`, `outcomes.py`, `strategy_replay.py`, schema |
| **Invariants** | Equal policy ID ≠ same episode/source (already in module header) |
| **Tests** | `test_evaluation_policy_manifest.py` plus persist tests |
| **Research benefit** | Makes later comparison *disclosed* |
| **Risk** | Operators treating policy_id as admission |
| **Depends on** | N1 optional but N2 can land first as a column; still UNBOUND as episode authority |

### Phase N3 — Consumer migration to `plan_version_id` (read paths only)

| | |
| --- | --- |
| **Problem** | P1 is latched but research/analytics/Telegram still count generations and geometry strings. |
| **Why now** | Foundation present but not consumed (R0 matrix B). |
| **Scope** | Research queries and P0-facing reports prefer `plan_version_id` when present; else UNAVAILABLE — never DISTINCT lifecycle_id as trades. |
| **Out of scope** | Changing public `event_key`; backfilling NULL historical IDs with guessed hashes |
| **Evidence** | Query output labels unavailable when IDs NULL |
| **Areas** | `queries.py`, evidence baseline audit consumers |
| **Invariants** | No operational decision feed |
| **Depends on** | P1/P3B1 (done) |

### Phase N4 — Analytics uniqueness / dual-outcome disclosure (still not canonical owner)

| | |
| --- | --- |
| **Problem** | `setup_outcome_analytics UNIQUE(lifecycle_id, final_outcome)` fans out vs progress. |
| **Why now** | R0 matrix L STILL OPEN. |
| **Scope** | Stop writing conflicting analytics rows **or** mark analytics as non-authoritative in every reader; do not invent global UNIQUE(plan_version_id) outcome (P3A forbids it without evaluation context). |
| **Out of scope** | P5 full reconciliation |
| **Depends on** | N2/N3 |

### Phase N5 — Observation admission (R0 implementation) — only after N1–N2

| | |
| --- | --- |
| **Problem** | No denominator. |
| **Why now** | Named as future unit; implementing it before witnesses/policy binding would mint empty admissions. |
| **Scope** | Explicit prospective admission distinct from plan lock and progress INSERT; continuation vs new episode; gap records when candles omitted. |
| **Out of scope** | Real user fills; 75% win-rate; short veto; CMC |
| **Invariants** | Admission not created because a TP exists; terminals not reopened by rescan |
| **Depends on** | N1, N2, R0 spec |

### Phase N6 — P5 independent tracking / outcome reconciliation

After N5. Continue evaluating admitted open plans or record gaps. Reconcile mature covered unambiguous outcomes without join fan-out. Still no expectancy dashboard as product truth.

### Phase N7 — P6 exact-as-run vs reconstruction split

After N1+N5. Reproduce decisions under named policy. Label reconstruction separately from original possession.

### Phase N8 — Expectancy (last)

Only on admitted, policy-bound, source-bound, non-censored populations with sample warnings. Never mix public delivery counts.

### Explicitly not a next phase

- Strategy parameter tuning
- Raising signal count
- Autonomous execution
- CMC as approval authority (shadow/context only, later, after N8 or as parallel observational stream that cannot promote lifecycle)
- Merging runtime and replay silently
- Live Runtime DB experiments on Dev PC

---

## SECTION 17 — STOP / GO GATES

| Gate | Question | Decision | Evidence |
| --- | --- | --- | --- |
| **A** | Change strategy / RR / confirmation / chop / sweep constants? | **STOP** | No empirical outcome cohort; 2026-08-15 strategy review still valid; phase40 guardrails freeze constants |
| **B** | Claim expectancy, win rate, or unique-trade counts? | **STOP** | P0 unique-trade UNAVAILABLE; R0 denominator missing; empty replay ≠ 0% |
| **C** | Treat replay as runtime authority or join replay_results to lifecycle outcomes as the same experiment? | **STOP** | EVAL_SEMANTICS non-equivalence; unbound policy |
| **D** | Continue gated public Telegram under dry-run defaults / Runtime with all gates? | **GO with constraints** | Defense-in-depth exists; lifecycle CONFIRMED ≠ public signal; Dev defaults dry-run/signals-off; do not weaken A/88/3R |
| **E** | Enable `ORDER_EXECUTION_ENABLED` or add place/withdraw? | **STOP** | Settings reject True; AGENTS.md; no order client |
| **F** | Implement CMC / regime / 2H as setup-generation or approval authority? | **STOP** | Workspace CMC rules; 2H informational; regime deferred in research contracts |
| **G** | Advance forensic persistence (witness, policy_id on new rows, consumer labels)? | **GO** | Contracts and tests already specify the hole; does not require strategy change |

---

## SECTION 18 — FUTURE RESEARCH OPPORTUNITIES

| Opportunity | Status | Prerequisite |
| --- | --- | --- |
| Exact-as-run lifecycle replay | **REQUIRES PREREQUISITES** | N1, N2, N5 |
| Honest expectancy / sample-aware R | **REQUIRES PREREQUISITES** | N5–N8 |
| Counter-market short quality in strong bull regimes | **REQUIRES PREREQUISITES** | Honest denominator first; currently a hypothesis in R0 §9 |
| 2H structure as a gate | **REQUIRES PREREQUISITES** | Empirical cohort; currently informational |
| Confirmation TF ≠ execution TF | **REQUIRES PREREQUISITES** | Measurement on admitted episodes |
| CMC derivatives/global/news shadow context | **REQUIRES PREREQUISITES** | Observational only; never setup authority (workspace rule) |
| Tick-quantized P1 IDs | **REQUIRES PREREQUISITES** | Verified tick on write path |
| Symbol universe exchangeInfo filters | **READY** as risk-reduction engineering, not as edge research | Does not need expectancy |
| Archive/capacity policy for SQLite | **READY** as ops | maintenance.py exists; live size unmeasured |
| Champion/challenger strategy | **NOT JUSTIFIED** | Would multiply episodes without a denominator |
| 75%+ accuracy product gate | **NOT JUSTIFIED** | Explicitly rejected in contracts |
| Always-in-market / lower RR for activity | **NOT JUSTIFIED** | Violates CCI principles |
| Autonomous execution | **NOT JUSTIFIED** | Out of product scope |

---

## SECTION 19 — TOP 10 RECOMMENDATIONS

1. **Treat P0 as the public research API.** Empty ≠ zero. Unique trades unavailable. Do not ship dashboards that DISTINCT `lifecycle_id`.
2. **Persist evaluator witnesses (N1)** so reconstruction is possible and labeled.
3. **Stamp new outcome and replay rows with evaluation_policy_id (N2)** without changing evaluator behavior.
4. **Reconcile documentation:** confirmed grade A vs B+; schema 25 vs docs saying 16/24; R0 matrix R vs STORAGE_SINGLE_COPY; hygiene v16 vs v25.
5. **Do not change strategy constants or RR floors** until admitted outcomes exist.
6. **Keep public delivery stricter than lifecycle CONFIRMED**; do not lower A/88/3R to match the stale B+ sentence.
7. **Migrate read paths toward `plan_version_id` when present** (N3); leave historical NULLs unresolved.
8. **Do not enable execution, withdrawals, or Dev-PC live Telegram/watch loops** from this work.
9. **Plan a later, behavior-preserving split of `telegram_lifecycle.py`** only with golden-file/gate tests; never as a “simplification” of policy.
10. **Measure Runtime DB size with the authorized read-only procedure** (`docs/post_restart_funnel_audit.md` / checkpoint readiness) before claiming storage risk is gone because of v25 refs.

---

## FINAL DECISION

Answers are blunt. They are about this SHA, not a future branch.

### 1. What is CCI today?

A **pre-alpha research system** with an **advanced-prototype** scanner, lifecycle, SQLite store, and gated Telegram path. It is not a trading bot and not an expectancy engine.

### 2. Is the liquidity-grab / MTF design actually implemented as intended?

**Partially.** Sweep+close-back and 15m BOS/CHoCH are real. 2.5R hard floor and 3R CONFIRMED/public floors are real. **2H structure does not gate.** Default confirmation TF equals execution TF. 2D is synthetic. Prefer 3–5R is scoring preference, not a hard reject of 2.5–3.0R setups (except CONFIRMED/public).

### 3. Are quality gates intact?

**Yes, in code.** Strategy, CONFIRMED, and public prefilter are layered and tested. The failure mode is **operator/doc confusion** (B+ vs A, Settings RR ≥ 0, opportunity 2.0R, `--disable-lifecycle`), not a missing CONFIRMED RR check.

### 4. Can replay reconstruct runtime decisions today?

**No.** Candles and decision timestamps are excluded from persistence. Replay is a related but non-equivalent simulator.

### 5. Can expectancy be honestly measured today?

**No.** No admission unit, no bound policy/source, dual outcome tables, research queries on replay rows, unique-trade unavailable.

### 6. Is public Telegram safe enough to keep using as a manual signal channel?

**Yes, with current defaults and gates**, as a **manual** channel. Weak setups can create **blocked audit rows**; live send requires multiple enables plus A/88/3R (watchlist) / confirmed prefilter. Dry-run cannot be bypassed on `TelegramSender`. Residual: Runtime misconfiguration; `AlertAgent` is a side path.

### 7. Should any strategy, RR, confirmation, or chop rule change now?

**No.**

### 8. Should autonomous order execution be enabled?

**No.** Never as a follow-on to this audit.

### 9. What is the single largest threat to CCI’s mission?

**False research confidence:** mixing deliveries, generations, replay fills, and simulations into “expectancy,” or claiming exact-as-run replay that the store cannot support.

### 10. What is the single most important next phase?

**Observational durability of what the evaluators saw** (witness + policy stamp on new rows), then consumer honesty (`plan_version_id` / UNAVAILABLE), then admission (R0), then P5/P6 — **not** more signals, not CMC authority, not execution.

**Overall: GO for gated manual intelligence operations and forensic persistence work. NO-GO for expectancy claims, strategy retunes, and execution.**

---

## Appendix A — Evidence index (primary files)

- Strategy: `app/strategies/liquidity_grab_pullback.py` (`LiquidityGrabEngine`, `detect_liquidity_sweep`, `detect_structure_shift`, `_analyze_structure_layer`)
- Scanner: `app/pipeline/scanner_runner.py` (`ScannerRunConfig`, `ScannerSymbolResult`, `structure_analysis_required`)
- Lifecycle: `app/lifecycle/{models,state_machine,service,outcomes,outcome_policy,outcome_events,eligibility,identity,economic_identity,hygiene,repositories,prefix_disposition_evidence}.py`
- Storage: `app/storage/{database,repositories,scan_payloads,run_provenance,maintenance,runtime_checkpoint_readiness}.py`
- Replay: `app/backtesting/strategy_replay.py`, `app/data/candle_integrity.py`
- Research: `app/research/{queries,evaluation_policy,source_evidence}.py`
- Evidence: `app/analytics/{evidence_contract,activation_accounting,outcome_ownership,performance_memory,setup_quality,public_signal_quality,public_alert_funnel}.py`
- Telegram: `app/alerts/{telegram_lifecycle,telegram_sender,telegram_outbox}.py`
- Config/safety: `app/core/{config,minimum_rr,trade_plan_integrity}.py`, `.env.example`, `.github/workflows/ci.yml`
- Contracts: `docs/research/*.md`
- Prior audits: `docs/audits/CCI_*.md`, `docs/confirmed_public_delivery_contract.md`

## Appendix B — Git forensic lineage (recent, FACT)

From `git log` at audited HEAD:

| Merge / work | Theme |
| --- | --- |
| PR #107 | P2A activation accounting |
| PR #108 | P2B CONFIRMED oscillation repair |
| PR #109 | P3A outcome ownership |
| PR #110 | P3B1 plan_version_id INSERT-only |
| PR #111 | P3B2A evaluation context |
| PR #112 | P3B2B durable eligibility cutoff |
| PR #113 | P3_PREFIX |
| PR #114 | R0 observation unit |
| PR #115 | EVAL_SEMANTICS |
| PR #116 | STORAGE_SINGLE_COPY (schema 25) |
| PR #118 | SOURCE_EVIDENCE_BOUNDARY |
| PR #119 | EVALUATION_POLICY_MANIFEST |
| PR #120 | Prospective batch-delivery capture (in-memory) |
| PR #121 | Runtime checkpoint readiness |

PR **#117** was closed without merge (`docs/research/evaluation_policy_manifest.md`) and is not authority.

## Appendix C — What this audit did not do

- Did not run `python -m pytest` (package missing in the audit VM).
- Did not open, copy, or query the live Runtime database.
- Did not call Binance, Telegram, or any network market API.
- Did not invent candles, outcomes, or disk-size measurements.
- Did not change application code.

---

*End of audit. Investigation only.*
