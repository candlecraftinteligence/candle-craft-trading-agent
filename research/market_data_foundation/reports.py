"""Human-readable A–L package. Online rows stay labeled when they are attached."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.market_data_foundation.cadence import cadence_contract
from research.market_data_foundation.catalog import inventory_rows
from research.market_data_foundation.constants import LOWERED_REASON, PHASE
from research.market_data_foundation.integrity import canonical_findings
from research.market_data_foundation.paths import repository_root


def render_package(
    *,
    online: dict[str, Any] | None = None,
    mcp: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, str]:
    meta = meta or {}
    findings = [item.as_dict() for item in canonical_findings()]
    inventory = inventory_rows()
    cadence = cadence_contract()
    return {
        "00_index.md": _index(meta),
        "00_plan.md": _plan(),
        "A_inventory.md": _inventory(inventory),
        "B_dependency_map.md": _dependency(inventory),
        "C_candle_volume.md": _family(findings, "candles", "Candle and volume integrity"),
        "D_oi_funding_positioning.md": _derivatives(findings),
        "E_freshness_missingness.md": _freshness(findings),
        "F_provider_matrix.md": _providers(),
        "G_cmc_mcp.md": _mcp(mcp),
        "H_top100.md": _top100(online),
        "I_shadow.md": _shadow(online),
        "J_cadence_cost.md": _cadence(cadence, online),
        "K_recommendations.md": _recommendations(),
        "L_review_packet.md": _review(meta),
        "evidence_contract.md": _contract(),
        "inventory.json": json.dumps(list(inventory), indent=2),
        "findings.json": json.dumps(findings, indent=2),
    }


def write_package(dest: Path, **kwargs: Any) -> Path:
    allowed = (repository_root() / "docs" / "research" / "market_data_foundation").resolve()
    resolved = dest.resolve()
    if resolved != allowed:
        raise ValueError("docs destination must be docs/research/market_data_foundation")
    resolved.mkdir(parents=True, exist_ok=True)
    for name, text in render_package(**kwargs).items():
        (resolved / name).write_text(text, encoding="utf-8")
    return resolved


def _index(meta: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# {PHASE}",
            "",
            "Research-only market-data evidence phase. Production gates, schema, and providers are unchanged.",
            "",
            f"Base SHA: `{meta.get('base_sha', '962dbbb35db942beef0423c0763555e665da668d')}`",
            f"Head SHA: `{meta.get('head_sha', 'the commit that contains this packet')}`",
            f"PR: {meta.get('pr_url', 'opened after push')}",
            "",
            "## Index",
            "",
            "- [Plan and safety](00_plan.md)",
            "- [A. Inventory](A_inventory.md)",
            "- [B. Dependency map](B_dependency_map.md)",
            "- [C. Candles and volume](C_candle_volume.md)",
            "- [D. OI, funding, positioning](D_oi_funding_positioning.md)",
            "- [E. Freshness and missingness](E_freshness_missingness.md)",
            "- [F. Provider matrix](F_provider_matrix.md)",
            "- [G. CMC MCP](G_cmc_mcp.md)",
            "- [H. Top-100 shadow](H_top100.md)",
            "- [I. Shadow comparison](I_shadow.md)",
            "- [J. Cadence and cost](J_cadence_cost.md)",
            "- [K. Recommendations](K_recommendations.md)",
            "- [L. Review packet](L_review_packet.md)",
            "- [Evidence contract](evidence_contract.md)",
            "",
            "Normalized observations may be written under gitignored `research_runs/market_data_foundation/`.",
            "Raw provider bodies are not stored. A hash does not rebuild a discarded payload.",
            "",
            "Runtime enablement for every production source is `unverified`. Code defaults are not deployment evidence.",
            "",
        ]
    )


def _plan() -> str:
    return "\n".join(
        [
            "# Implementation plan",
            "",
            "Inspect the existing clients, normalizers, cache, freshness policy, universe builder, and context consumers.",
            "Add a research package that production `app` code does not import.",
            "Reuse pure CCI functions and injected transports. Do not add a provider registry, schema migration, or scanner change.",
            "",
            "## Safety",
            "",
            "- No order execution, transfers, Telegram delivery, scanner watch loop, or live database.",
            "- Output paths must resolve inside the DEV research root.",
            "- Public quality score 88, grade A, public RR 3, internal minimum RR 2.5, schema 27, and zero freshness allowance stay as they are.",
            "- CMC is a shadow reader. CoinPaprika remains the production rank and dominance source.",
            "- Online caps are lowered because spare shared-IP capacity cannot be shown.",
            f"- Lowered-budget reason: {LOWERED_REASON}.",
            "",
            "## Tests",
            "",
            "Deterministic tests cover integrity, CMC envelopes, the rank boundary, budgets, path guards, and unchanged gate constants.",
            "Live HTTP is opt-in through the CLI and is not part of `python -m pytest`.",
            "",
            "## Completion",
            "",
            "A runnable offline audit, committed sanitized contracts, and an honest online result or blocker.",
            "",
        ]
    )


def _inventory(rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    lines = [
        "# A. Data-source inventory",
        "",
        "Each row is code-present at the pinned lines. Runtime enablement is unverified.",
        "Expectancy benefit is `not established` unless an existing measured result is cited. None is cited here.",
        "",
    ]
    for row in rows:
        lines.append(f"## {row['metric_id']}")
        lines.append("")
        for key in (
            "provider",
            "api_version",
            "endpoint",
            "parameters",
            "direct_or_aggregate",
            "venue",
            "instrument_type",
            "contract_id",
            "currencies",
            "raw_fields",
            "native_unit",
            "normalized_unit",
            "conversion",
            "timestamps",
            "refresh",
            "history",
            "fetcher",
            "normalizer",
            "cache",
            "consumer",
            "role",
            "missing_behavior",
            "code_present",
            "dev_exercised",
            "runtime_enabled",
            "expectancy_benefit",
        ):
            lines.append(f"- {key}: {row[key]}")
        lines.append("- code refs:")
        for ref in row["code_refs"]:
            lines.append(f"  - `{ref['path']}:{ref['line']}` contains `{ref['contains']}`")
        lines.append("")
    return "\n".join(lines)


def _dependency(rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    lines = [
        "# B. Source-to-decision map",
        "",
        "Path shape: fetcher, normalizer, cache, consumer, gate or research output.",
        "Optional flags defaulting to false are not Runtime proof. `runtime_enabled` is unverified throughout.",
        "",
        "| metric | cache / missing behavior | decision role |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['metric_id']} | {row['cache']}; {row['missing_behavior']} | {row['role']} |"
        )
    lines.extend(
        [
            "",
            "Neutral defaults that can hide missingness:",
            "",
            "- Open-interest `unit` defaults to N/A, while the numeric field can still be present. A present number with unit N/A is not a verified unit.",
            "- Bybit candle `close_timestamp` stays N/A. Freshness derives the scheduled close from the open. That derived close is not a venue publication time.",
            "- Bybit ticker timestamp copies the response envelope time.",
            "- An empty liquidation stream is not zero.",
            "- Cache TTL does not extend the freshness allowance. The allowance is zero.",
            "- USDT turnover is not USD market cap.",
            "",
            "ETH has no standalone context service. Regime fetches ETHUSDT only when `market_regime_enabled` is true, and that flag defaults to false.",
            "USDT dominance is an explicit N/A input in the regime classifier.",
            "Sector context is research-only and does not recalculate a trading decision.",
            "",
        ]
    )
    return "\n".join(lines)


def _family(findings: list[dict[str, Any]], family: str, title: str) -> str:
    selected = [item for item in findings if item["family"] == family or item["family"] == "temporal"]
    return _finding_doc(title, selected) + (
        "\n\nNo-trade bars are present intervals with zero volume and flat prices. "
        "A missing interval is a continuity gap. Those are different dispositions.\n\n"
        "12h and 1d comparisons use native candles. The production resampler accepts only 2d. "
        "A 2d result is fresh only when its daily inputs are admissible at the cutoff.\n\n"
        "USDT is not converted to USD. A `1000` contract prefix is a different instrument and is not stripped.\n"
    )


def _derivatives(findings: list[dict[str, Any]]) -> str:
    selected = [
        item
        for item in findings
        if item["family"] in {"open_interest", "funding", "order_book", "liquidations", "prices"}
    ]
    body = _finding_doc("D. Open interest, funding, positioning, book, and liquidations", selected)
    body += """
## Numerical contracts

Linear example, synthetic: 2.5 base units at mark 100000 USDT is 250000 USDT notional. At mark 110000 the notional is 275000 USDT and the base position change is 0. That notional change is not position growth. The quote unit stays USDT.

Inverse example, synthetic: 1000 contracts with a 1 USD face at price 100000 is 0.01 coin. At price 50000 the coin exposure is 0.02 while the contract count is unchanged.

Funding example: fraction 0.0001 is 0.01 percent and 1 basis point. A fraction at or above 1 is incompatible with the usual per-interval fraction convention and is not divided by 100 automatically.

Bybit `openInterest` and `singleOpenInterest` are both retained in the diagnosis. The repository stores `openInterest` and does not divide by two. If the fields differ, or the DTO unit is N/A, the counting convention is unverified.

Binance `globalLongShortAccountRatio` and Bybit `account-ratio` are account populations. They are not top-trader position ratios and not taker flow.

Order-book synchronization is not applicable for a snapshot that has no diff sequence. A partial liquidation stream does not prove total liquidations. A modelled heatmap is not an observed event.

Production incident status for these code contracts: not established.

The derivatives path continues through `_fetch_optional_market_data`, `_derivatives_enrichment_input`, and `_derivatives_rejection` to `REJECTED_BY_DERIVATIVES`. Current funding and open interest are numbers. Their source timestamps are not fields on that input. Previous open interest is `history[-2]`. A rejection can therefore be computed from stale or future inputs. That is a recorded gap. The gate is unchanged.

`liquidation_observation` is a research helper. The production coverage cases below come from `SymbolLiquidationAggregator`.
"""
    from research.market_data_foundation.consumers import (
        derivatives_timestamp_probe,
        production_liquidation_coverage_probe,
    )

    body += "\n```json\n" + json.dumps(derivatives_timestamp_probe(), indent=2) + "\n```\n"
    body += "\n```json\n" + json.dumps(production_liquidation_coverage_probe(), indent=2) + "\n```\n"
    return body


def _freshness(findings: list[dict[str, Any]]) -> str:
    selected = [item for item in findings if item["family"] in {"temporal", "candles"}]
    body = _finding_doc("E. Freshness, provenance, and original availability", selected)
    body += """
Essential freshness remains `essential-candle-freshness-v1` with a zero allowance and a Unix-epoch UTC anchor, including synthetic 2d candles.

A candle open is not its close. A response envelope time is not a measurement time. A cache insertion time is not a producer time. A regime computed now is stale when its candle input is stale.

Historical reconstruction requires both an admissible measurement time and proof CCI had the input at the decision cutoff. A later fetch can support a structural check. It cannot prove the earlier decision saw that bar.

Cache kinds used by the existing cache are miss, hit, and expiry refetch. A failed fetch raises and is not stored. The cache key includes the exchange name, so Binance and Bybit entries do not share a key when callers pass that name.

A connected liquidation snapshot uses the snapshot clock as `observed_at`. That clock is not an event measurement time.
"""
    return body


def _providers() -> str:
    return """# F. Provider matrix

Access date: 2026-10-19. Pages below were read that day. A missing cell is unresolved, not a guessed quota. No subscription was purchased. Live sampling is skipped because shared-IP spare capacity is unknown.

| endpoint | history / resolution | rate / entitlement | cost / rights | unresolved |
| --- | --- | --- | --- | --- |
| Binance USD-M `GET /fapi/v1/klines` | repository cap `BINANCE_KLINE_LIMIT_MAX = 1500`. Official weight table was not retrieved: the developer page returned a bot wall. | public REST. Shared IP weight is cumulative and was not isolated. | public market data. Redistribution of stored bodies is not established. | current official request weight by limit |
| Bybit `GET /v5/market/kline` | intervals `1,3,5,15,30,60,120,240,360,720,D,W,M`. Limit 1 to 1000, default 200. Newest first. Linear volume is base coin; turnover is quote. Retrieved from the V5 kline page. | public. Category defaults to linear. IP rate on this page was not stated. | public market data. Redistribution not established. | numeric IP rate limit |
| Bybit `GET /v5/market/open-interest` | `openInterest` is both sides; `singleOpenInterest` is one side. Linear BTCUSDT unit is BTC. | public. Not called for a new sample in this correction. | no overage call | cross-venue unit equivalence |
| CoinPaprika `GET /v1/tickers` | FAQ: REST price resolution is up to 10, 3, or 1 minute by plan. Price is a volume-weighted aggregate, not one venue. | FAQ: 10 requests/second per IP. Further quotas are plan-specific. HTTP 429 or 402 at the limit. | FAQ: commercial use inside the customer company is for paid packages. Redistribution requires Enterprise and a contract. CCI's current keyless call is not a paid package. | free-tier monthly quota; whether keyless `/v1/tickers` is inside the 10/second pool only |
| CMC keyless `GET /v3/cryptocurrency/listings/latest` and `GET /v1/global-metrics/quotes/latest` | listings page is a current rank snapshot, not a history archive. CMC100 is a different index. | keyless GET, no API-key header, IP rate pool. No numeric quota on the keyless page. | commercial redistribution not established. Authenticated REST uses `X-CMC_PRO_API_KEY` and was not called. `/v5` derivatives market pairs are not on the keyless list. | numeric keyless quota and license for stored rows |
| CMC MCP | not a REST history source | separate `X-CMC-MCP-API-KEY`. Two prior reads are implementer-reported only. | no new paid call | independent connection proof |
| CoinGlass `GET /api/futures/liquidation/history` | intervals include 1m through 1w. Limit default and max 1000. Time is milliseconds. Values are USD notionals. Plan interval gate: Hobbyist `>=4h`, Startup `>=30m`, Standard and above no interval limit. Retrieved from the liquidation-history reference. | header `CG-API-KEY`. Not called. | pricing page: Hobbyist $29/month or $348/year, 80+ endpoints, 30 requests/minute, updates at most 1 minute, personal use. Startup $79/month or $948/year. Higher plan prices and the history-range column headers did not survive the page text. | exact history-range column for each plan; Enterprise price; whether USD liquidations may be retained |

CoinGlass remains disabled. There is no verified no-overage allowance.

Same-venue exchange data remains the candidate authority for that venue. CoinPaprika stays the production rank source. This matrix is not a provider rollout.
"""


def _mcp(mcp: dict[str, Any] | None) -> str:
    if not mcp:
        return """# G. CMC MCP

Status: `implementer-reported and independently unverified`.

The earlier session reported a tool list and two read-only calls. Sanitized protocol schemas and response receipts were not retained in this repository. That prose is not independent proof of a connection. No additional MCP call is made here. Keyless REST does not imply keyless MCP. This CLI does not read or send an MCP key.
"""
    status = mcp.get("status", "blocked")
    tools = mcp.get("tools") or []
    lines = [
        "# G. CMC MCP",
        "",
        f"Status: `{status}`.",
        "",
        mcp.get("note", ""),
        "",
        "Tool names returned by the live session:",
        "",
    ]
    for name in tools:
        lines.append(f"- `{name}`")
    lines.append("")
    lines.append("Narrative tool output is not deterministic evidence and is not a CCI gate input.")
    lines.append("")
    return "\n".join(lines)


def _top100(online: dict[str, Any] | None) -> str:
    base = """# H. Global Top-100 shadow

The boundary is ranks 1 through 100 before exclusions. Rank 101 is never used to refill a stable, wrapped, ambiguous, or unsupported asset.

Identity is a CMC-id crosswalk with source, retrieval time, and record id. A unique ticker is only an unverified candidate. It does not increment identity-validated or eligible counts. Quote, settlement, contract type, and multiplier must match the crosswalk. Prefixes are not stripped. Wrapped assets are not merged.

The earlier live summary's 100 validated and 74 eligible figures were ticker joins without that crosswalk. Those counts are withdrawn. They are not restated as validated identity.

An incomplete or temporally invalid ranking does not feed the sample queue. Structural rank completeness and timestamp validity are separate statuses.

Owner monitoring is a separate count. This phase does not execute the production scanner, so scan attempts are zero.
"""
    if not online or not online.get("top100"):
        return base + "\nLive Top-100 summary: not attached.\n"
    return base + "\n```json\n" + json.dumps(online["top100"], indent=2) + "\n```\n"


def _shadow(online: dict[str, Any] | None) -> str:
    text = """# I. Shadow comparison

Offline probes, all synthetic or pure-function:

- Public gate: score 88 and grade A passes; score 87 fails the score gate; grade B fails the grade gate; unevaluated quality fails.
- Freshness: a covered 1m series passes; removing the latest bar is stale. Allowance stays zero.
- Open interest: a price-only notional change is not position growth. Missing previous OI does not become zero.
- Cache: a failed fetch is not stored; a hit keeps the measurement time distinct from cache bookkeeping time; Binance and Bybit keys stay separate.
- Candle pairing uses exact open timestamps. Unmatched rows are kept.

Same-venue vendor comparison is not applicable unless a vendor payload identifies the venue and the sample summary says it was fetched. Cross-venue differences are context. Symbol-only CoinPaprika overlap is not validated identity.

The representative offline comparison below is synthetic. Captured prices are unavailable because raw bodies were not retained. Live comparison was not repeated.
"""
    from research.market_data_foundation.study import offline_metric_comparison

    text += "\n```json\n" + json.dumps(offline_metric_comparison(), indent=2) + "\n```\n"
    if online and online.get("btc_cross_venue"):
        text += "\nAttached cross-venue BTC note:\n\n```json\n"
        text += json.dumps(online["btc_cross_venue"], indent=2)
        text += "\n```\n"
    else:
        text += "\nLive cross-venue sample: not attached.\n"
    return text


def _cadence(cadence: dict[str, Any], online: dict[str, Any] | None) -> str:
    text = (
        "# J. Cadence and cost\n\n"
        "The watch scheduler is fixed start-to-start. "
        f"The CLI default interval is {cadence['cli_default_interval_seconds']} seconds, "
        "so a five-minute cycle is not the code default. "
        "A 300-second sleep-after-completion story does not describe `schedule_after_iteration`.\n\n"
        "```json\n"
        + json.dumps(cadence, indent=2)
        + "\n```\n\n"
        "The 30-symbol freshness microbenchmark is not the selected-universe benchmark. "
        "The configured-workload benchmark below counts scanner kline and optional reads and times a local freshness plus 2d resample stage. "
        "It does not call the network and does not claim five-minute headroom.\n\n"
        "Cost projection uses 288 cycles per day only as a what-if for a 300-second cadence, "
        "and a 30-day month. Request counts are not billing credits. "
        "The code-default 60-second interval would be 1440 cycles per day if that default were the Runtime setting. "
        "Runtime cadence is unverified.\n"
    )
    from research.market_data_foundation.study import selected_universe_benchmark

    bench = selected_universe_benchmark(("BTCUSDT", "ETHUSDT"))
    published = {
        key: value
        for key, value in bench.items()
        if key not in {"plan", "local_stage_seconds"}
    }
    text += "\nConfigured-universe local benchmark:\n\n```json\n" + json.dumps(published, indent=2) + "\n```\n"
    if online and online.get("budget"):
        text += "\nAttached request accounting:\n\n```json\n" + json.dumps(online["budget"], indent=2) + "\n```\n"
    if online and online.get("benchmark"):
        text += "\nOffline freshness microbenchmark:\n\n```json\n" + json.dumps(online["benchmark"], indent=2) + "\n```\n"
    if online and online.get("latency_note"):
        text += "\n" + str(online["latency_note"]) + "\n"
    return text


def _recommendations() -> str:
    return """# K. Recommendations

These are future tests, not production changes from this phase.

| measurement | authority to test later | why it is not changed now | smallest next validation |
| --- | --- | --- | --- |
| Binance or Bybit contract candles, volume, mark, index, funding, OI, book | that same venue | no sample here shows a vendor is more correct for that contract | exact-timestamp same-venue comparison with units |
| BTC and ETH direction | same-venue closed candles already used by CCI | regime and BTC context already have lineage; ETH context is only the regime fetch | confirm Runtime actually enables the flags before relying on them |
| Global rank and dominance | keep CoinPaprika in production | CMC IDs and dominance are candidates, not a proven replacement | aligned-window overlap with identity checks, then a separate adoption review |
| Liquidations | observed venue events with coverage limits | aggregates and heatmaps are not observations | a no-overage historical sample only after rights and retention are known |
| Macro | original source with vintage | already present and not a setup authority | no gate change |

Central question, by consumer:

| consumer | verdict | evidence |
| --- | --- | --- |
| Essential candle freshness policy | supported within tested scope | zero allowance, epoch anchor, and existing freshness tests; this phase does not widen it |
| Shared candle OHLC and negative-volume boundary | verified code gap, not a proven production incident | `validate_candle_sequence` does not check those fields; later consumers do some of the checks |
| Bybit OI unit and both-side versus single-side | insufficient evidence | DTO unit stays N/A; differing fields are not halved and stay unverified |
| Funding as last settled history | supported within tested scope for that meaning | client reads funding history, not premium index |
| Predicted funding | insufficient evidence | endpoint is not called |
| Public score 88, grade A, public RR 3 | supported within tested scope | constants reasserted; unevaluated quality does not pass |
| Top-100 boundary in the strict universe | supported within tested scope of the current builder and the shadow counter | rank above N is excluded and not refilled |
| Runtime data correctness on 2026-10-18 | insufficient evidence | user-reported cycles were not re-observed and are not treated as proof |
| Signal expectancy | insufficient evidence | this phase is not an expectancy study |

No source comparison here establishes a signal-performance improvement.
"""


def _review(meta: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# L. Review packet",
            "",
            f"- Repository: `candlecraftinteligence/candle-craft-trading-agent`",
            f"- Base SHA: `{meta.get('base_sha', '962dbbb35db942beef0423c0763555e665da668d')}`",
            f"- Head SHA: `{meta.get('head_sha', 'the commit that contains this packet')}`",
            f"- PR: {meta.get('pr_url', 'https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/136')}",
            f"- Tests: {meta.get('tests', '3116 passed, 1 warning, Python 3.11.9, 2026-10-19. Required CI on this commit is authoritative.')}",
            f"- CI: {meta.get('ci', 'required GitHub check on this exact commit; an earlier green run does not cover it')}",
            "",
            "Production paths touched: none. Scanner, config, schema, strategy, Telegram, and lifecycle code are unchanged.",
            "",
            "Astra verdict on `a8c237529599089678e2029f51de9d2a40857313`: REQUIRES_CORRECTION for one remaining rate-limit hole. This packet stops a provider after its terminal HTTP 429. It is not approval.",
            "",
            "| id | resolution | remaining limit |",
            "| --- | --- | --- |",
            "| R1 | CMC-id crosswalk required; ticker-only matches stay unverified and unqueued. Settlement, quote, type, and multiplier are checked. Prior 100/74 claims withdrawn. | No retained crosswalk for the earlier live page, so that page is not re-certified. |",
            "| R2 / B1 | Structural completeness, numeric domain, and decision-time admissibility are separate. Invalid quotes and late receipt or source times do not queue, even with a valid crosswalk. | The six-hour window is a research bound, not a production allowance. |",
            "| R3 / B2 | Transport, decode, parser, and domain results are stored on the observation. HTTP 200 is not verified evidence. A partial summary is still written. | None for the injected cases. |",
            "| R4 / B3 | Replay reproduces normalized global metrics from checksummed observations and rejects failed, late, and schema-invalid rows. Raw bodies stay unretained. | MCP connection remains implementer-reported and independently unverified. No new MCP call. |",
            "| R5 / B4 | Received bytes include discarded chunks and stop the study. A terminal HTTP 429 stops that provider for the rest of the finite run, including when retries are zero. Retry-After is honored in full or the retry is not attempted. Unrelated providers continue. Live sampling stays off. | Shared-IP spare capacity is unknown. CoinGlass stays disabled. |",
            "| R6 | `depth_update_gap` removed. Tests use `SynchronizedLocalOrderBook`. | Production book code unchanged. |",
            "| R7 | Funding and OI are traced through enrichment and `REJECTED_BY_DERIVATIVES`. Liquidation cases use `SymbolLiquidationAggregator`. | Source timestamps are still dropped before rejection. That gap is recorded, not patched. |",
            "| R8 | Synthetic price, volume, OI, and funding comparison plus a configured-request benchmark. Provider matrix uses the 2026-10-19 documents. | Live metrics and Binance weight table remain unresolved. CoinGlass higher-plan prices are incomplete. |",
            "",
            "Independent reviewer: Astra. This document is not Astra approval. A new head needs a new review.",
            "",
            "Allowed next action after a verdict: only what the verdict names. No Runtime pull, restart, or database action is authorized by this phase.",
            "",
        ]
    )


def _contract() -> str:
    return """# Evidence contract

An observation carries run id, code SHA, policy digest, provider, endpoint, sanitized parameters, venue and instrument ids, native and normalized value and unit, timestamps, interval semantics, cache status, lineage, response status, latency, retries, request weight when a header actually provides it, raw hash when the body may be kept locally, and a disposition.

Dispositions: verified, missing, stale, malformed, incompatible, unverified, unsupported, access_blocked, not_applicable.

CCI marks: missing, stale, unsupported, and not_applicable display as N/A. Unverified, malformed, incompatible, and access_blocked display as Unverified. Missing is never the number zero.

Separate dimensions, which a single HTTP 200 does not satisfy: transport success, field presence, semantic validity, identity compatibility, temporal coverage, original-decision availability, and sufficiency for a named consumer.

Publication latency is reported only when a real publication timestamp exists. Otherwise the limitation is named.
"""


def _finding_doc(title: str, findings: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", ""]
    if not findings:
        lines.append("No findings in this family.")
        return "\n".join(lines)
    for item in findings:
        lines.extend(
            [
                f"## {item['finding_id']}",
                "",
                f"- classification: {item['classification']}",
                f"- severity: {item['severity']}",
                f"- production incident: {item['production_incident']}",
                f"- summary: {item['summary']}",
                f"- expected: {item['expected']}",
                f"- observed: {item['observed']}",
                f"- consumers: {', '.join(item['consumers'])}",
                f"- evidence: {', '.join(item['evidence_ids'])}",
                f"- follow-up: {item['follow_up']}",
                f"- reproduction: {item['reproduction']}",
                "",
            ]
        )
    return "\n".join(lines)
