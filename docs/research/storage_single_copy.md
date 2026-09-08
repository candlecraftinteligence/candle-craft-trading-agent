# STORAGE_SINGLE_COPY — Prospective lossless scan-result deduplication

Application schema **v25**. Historical v24 payloads are unchanged.

## Problem

`store_scan_result` persisted a full scan payload in `scan_runs.raw_payload_json` and independently persisted each symbol payload in `symbol_results.raw_result_json`. Ordinary default, CLI, and watch producers write the same serialized result twice. This phase removes that second copy **prospectively** when exact equality is proved.

This is a storage representation change. A reference identifies a physical stored result within one run. It is not an economic observation, admission, episode, trade, policy identity, or source-authority claim.

## Formats

`scan_runs.raw_payload_format TEXT NOT NULL DEFAULT 'inline_v1'` is the discriminator. Do not infer format from payload shape.

| Format | Physical `raw_payload_json` |
| --- | --- |
| `inline_v1` | The complete logical payload (legacy and fallback). |
| `symbol_refs_v1` | Top-level fields retained; `results[]` is an ordered list of `{symbol, sha256}` only. |

Each `symbol_refs_v1` target is the unique `symbol_results` row for the enclosing `run_id` and the exact symbol string. `sha256` is over the UTF-8 bytes of that row's `raw_result_json` as stored. No cross-run, latest-row, or name-normalized lookup.

## Eligibility and fallback

A new run may use `symbol_refs_v1` only when:

1. `results` is a nonempty list of mappings with valid exact symbol strings.
2. Those symbols are unique and 1:1 with the symbol records prepared for the same run. Original result order is preserved even when it differs from insert order.
3. The existing `_json_dump` serializer of each raw result equals that symbol's prepared `raw_result_json` byte-for-byte.
4. The compact UTF-8 representation is smaller than the inline representation.

Otherwise the **entire** original payload is stored as `inline_v1`. No repair, partial deduplication, reordering, or summary changes. A payload that was previously accepted is not rejected because it is not compactable. Duplicate `run_id` / child uniqueness failures still fail.

Scalar summaries (`_bucket_counts`, `_lifecycle_state_counts`, `_a_grade_actionability_counts`, watch counters, provenance) are computed from the original logical payload **before** references are substituted.

## Reader

`app.storage.scan_payloads.load_logical_scan_payload` reconstructs the logical payload from a caller-owned connection:

- Legacy schema without the format column: treat stored JSON as `inline_v1`. Readers do not migrate.
- Schema with the column: the stored format value is required.
- `symbol_refs_v1` loads that run's children in one bounded query, checks membership and hashes, and restores `results` in manifest order.
- Integrity failures raise `ScanPayloadIntegrityError`. They must not become an empty run, invented N/A payload, or partial success.

Rollback of **prospective encoding** is `SCAN_RAW_PAYLOAD_INLINE_ONLY=true` / `store_scan_result(..., inline_raw_payload=True)` on a decoder-capable release. Rolling back to an unmodified v24 reader after referenced rows exist is **not** supported. Do not downgrade `user_version` or rewrite history.

## What this does not do

No live-database migration, VACUUM, historical rewrite, nested deduplication, compression, admission, source/policy binding, canonical outcomes, or expectancy claims. Current live size and runway remain unmeasured.
