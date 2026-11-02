# Combined rollout prerequisite repair — implementation plan

Authorized scope: DEVELOPMENT only, based on
`0086e9e508e9b4b75f1a6c0c0c8210be5260c326`, branch
`codex/cci-v2-combined-prerequisites`. Existing checkouts and main stay untouched.
No Runtime filesystem/database access, services, authenticated acquisition,
Telegram polling/delivery, strategy changes, identity approval or deployment.

## Files and behavior

1. `scripts/cci_v2_release_profiles.py`, new
   `app/operations/release_activation.py`, `tests/test_release_profiles.py` and
   `tests/test_combined_release_activation.py`: retain original hash-bound
   profile, complete identity, paths and arguments. Derive only bounded feature
   changes from an explicit reviewed activation packet. Bind launch approval to
   exact proposal/contract/role/SHA/tree/identity/differences and an expiry.
   Reject unknown keys, CMC decision authority, provider changes, path drift,
   stale approval and inherited feature overrides. Existing legacy profiles
   remain usable only through their existing inactive feature contract.

2. `app/cmc/client.py`, `collection.py`, new `acquisition.py` and
   `restart_cache.py`, `app/cmc/storage.py`, `tests/test_combined_cmc_acquisition.py`
   and `tests/test_combined_cmc_cache.py`: acquire ID maps without a whole-batch
   symbol filter; use bounded unfiltered pagination, retaining exact numeric IDs,
   slugs and Unicode native identities. Validate shared pair page completeness,
   duplicate identities and consistent exchange metadata. Persist bounded page
   fragments with a last-written family manifest; incomplete, corrupt or evicted
   families are never restored as complete. No storage cap increases.

3. `app/cmc/scanner_context.py`, `runtime.py`,
   `app/pipeline/scanner_runner.py`, `tests/test_combined_cmc_status.py`:
   every SHADOW analyzed result gets explicit research-only status even when
   construction or acquisition fails. Separate mapping, global context, contract
   context, stale/unavailable, budget, failure and pending states. Keep original
   decision timestamps and public eligibility unchanged.

4. `app/cmc/identity_review.py`, new offline
   `scripts/cci_v2_prerequisite_evidence.py`,
   `config/cmc/combined-shadow.collection.json`,
   `tests/test_combined_prerequisite_evidence.py`, and
   `docs/operations/CCI_V2_COMBINED_PREREQUISITE_HANDOFF.md` plus bounded JSON
   receipts: generate an owner-review inventory from available evidence without
   approving bindings. Explicitly withhold multipliers, ARB exclusions, Unicode
   uncertainties, OI/funding/index semantics. Record provenance and missing
   current evidence as N/A. Validate 121/day and 363/day planning separately
   from provider account usage, validation ledgers and existing reservations.

5. New `app/operations/rollback_compatibility.py`,
   `tests/test_combined_rollback_safety.py`: pure retained-intent capability
   assessment, with synthetic actual persistence/restart/transport tests for
   rich, legacy, pending/retryable/in-flight/uncertain and canonical dedup/offset
   continuity. Never rewrite or replay reserved public messages. Retain current
   compatible code for CMC-off and new-message legacy fallbacks; require an exact
   rich-compatible target proof before code rollback.

6. New `scripts/cci_v2_resource_probe.py` and
   `tests/test_combined_resource_probe.py`: bounded offline synthetic collector,
   page-cache and restart workload, process peak RSS/allocations/SQLite receipts.
   Define measured prestart host criteria separately from synthetic results;
   no automatic host changes or Runtime capacity claims.

## Critical failure conditions and verification

Fail closed on altered launch arguments, any identity field, changed source
hashes, unknown configuration, stale/excessive approval, partial pagination,
ambiguous/colliding IDs, unapproved contracts, changed acquisition/source time,
cache partial writes/eviction, budget exhaustion, unsupported retained transport
formats, or insufficient measured host capacity. SHADOW has no signal authority.

First run existing release/CMC/stream/precision regressions on the exact baseline.
Add deterministic mock-provider and isolated SQLite tests for every new feature.
Run focused regressions, full Windows Python 3.12 suite using the existing project
environment, compileall, pip check and git diff --check. Open one PR and obtain
Linux/Windows CI receipts on the exact candidate commit. Do not weaken tests or
claim any unavailable evidence as verified. Stop for owner review after the PR.

Latest 55-symbol research packet was not present in the initial committed file
inventory; its DEV location has been requested while independent work continues.
The older committed captures are historical research only.

Implementation findings: the owner supplied the 2026-11-02 NO-GO narrative during
work; its expanded per-row capture was not supplied. The review inventory labels
the committed 2026-11-01 roster separately and does not manufacture missing IDs.
The exact CUSTOM schedule uses global metrics and liquidations every 1,800 s,
four pair pages every 14,400 s, daily map/registry, 20 requests/minute and 1,000
observations. A synthetic restart exposed timezone-dependent nested-model repr
hashing in `app/cmc/normalize.py`; canonical instrument-field hashing is included
to make identical restored contract observations idempotent. The existing source
and storage freshness limits are unchanged.
`app/cmc/identity.py` also explicitly withholds non-unit bindings and the three
known unresolved multiplier contracts at instrument admission, even if their
identity has owner approval. Identity approval cannot verify multiplier semantics.
Exact manifest-ID coverage is tracked independently of whole-catalog pagination:
unrelated unacquired listings cannot invalidate exact observed numeric IDs, while
missing requested IDs, duplicates or incomplete exchange-pair pages withhold
contract enrichment. Global map pagination remains explicitly partial when bounded.
Source inventory anchors in `research/market_data_foundation/catalog.py` and its
two generated artifacts track the changed scanner line numbers. Collector
timeout status is exposed through a research view, preserving the original
frozen acquired snapshot and all deadline/cancellation guarantees.
