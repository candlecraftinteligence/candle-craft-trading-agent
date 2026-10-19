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
            f"Head SHA: `{meta.get('head_sha', 'recorded at commit')}`",
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
            "Raw provider payloads, when a live round succeeds, stay under gitignored `research_runs/market_data_foundation/`.",
            "A hash in this package does not by itself let a reviewer rebuild a payload that was not committed.",
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
"""
    return body


def _freshness(findings: list[dict[str, Any]]) -> str:
    selected = [item for item in findings if item["family"] in {"temporal", "candles"}]
    body = _finding_doc("E. Freshness, provenance, and original availability", selected)
    body += """
Essential freshness remains `essential-candle-freshness-v1` with a zero allowance and a Unix-epoch UTC anchor, including synthetic 2d candles.

A candle open is not its close. A response envelope time is not a measurement time. A cache insertion time is not a producer time. A regime computed now is stale when its candle input is stale.

Historical reconstruction requires both an admissible measurement time and proof CCI had the input at the decision cutoff. A later fetch can support a structural check. It cannot prove the earlier decision saw that bar.

Cache kinds used by the existing cache are miss, hit, and expiry refetch. A failed fetch raises and is not stored. The cache key includes the exchange name, so Binance and Bybit entries do not share a key when callers pass that name.
"""
    return body


def _providers() -> str:
    return """# F. Provider matrix

Access date for this document: 2026-10-19. Seed URLs are the assignment's primary docs. A live call is recorded only in the attached online summary. Documentation availability is not an entitlement.

| interface | role in CCI | exercised by this phase | rights / cost | blocker |
| --- | --- | --- | --- | --- |
| Binance USD-M public REST | production candles, ticker, funding history, OI, account ratio | code audit; live only if the sample summary says observed | public market data. Commercial redistribution of stored payloads is not established here. | none for public REST; shared IP spare capacity unknown |
| Bybit V5 public REST | production linear client | code audit; live only if the sample summary says observed | public market data. Commercial redistribution not established. | OI unit and single-versus-both convention not proven from the repository DTO |
| CoinPaprika v1 | production ranks and BTC dominance | code audit; live only if the sample summary says observed | public API. FAQ should be re-read before publishing stored tickers. | full ticker payload may exceed the response cap |
| CMC keyless REST | shadow listings and global metrics only | shadow adapter; live only if the sample summary says observed | keyless GET, no key header. Numeric free quota is not invented. Commercial use and redistribution need a later reading of the current license. | not a production source |
| CMC authenticated REST and derivatives | not integrated | not called | `X-CMC_PRO_API_KEY` is a different credential from MCP. Derivatives market pairs are not assumed keyless. | no entitlement used |
| CMC MCP | research interface only | see G | separate header and entitlement | configuration is not a connection |
| CoinGlass | not integrated | documentation and synthetic contracts only | plan resolution, retention, and commercial rights are unresolved | no authorized no-overage entitlement, so no call |

Revalidated on 2026-10-19 from the keyless public API page: the root is `https://pro-api.coinmarketcap.com/public-api`, GET only, no API-key header, and the rate pool is IP-based. The page does not state a numeric free quota. It documents `/v3/cryptocurrency/listings/latest` and `/v1/global-metrics/quotes/latest`, plus the canonical map and metadata paths. CMC100 is a separate index endpoint, not the Top-100 listings. The keyless standard list on that page does not include the `/v5` derivatives market-pair routes, so those remain untested and not assumed keyless. Authenticated REST still uses `X-CMC_PRO_API_KEY`, which was not sent.

Bybit's open-interest page, retrieved the same day, defines `openInterest` as the sum of both sides and `singleOpenInterest` as one side. For linear BTCUSDT the documented unit is BTC. Inverse BTCUSD is documented in USD. The repository stores `openInterest` and leaves `unit` as N/A.

Same-venue exchange data remains the candidate authority for that venue's candles, trades, mark, index, funding, OI, positioning, taker flow, and book. That is a recommendation to test, not a result of this sample.

Global rank and dominance are methodology measurements. CoinPaprika stays in production during this phase.

Questions for a later licensing pass, not sent to the providers: whether stored CMC or CoinPaprika responses may be committed; whether derived commercial signals require a paid contract; whether CoinGlass historical liquidations can be retained.
"""


def _mcp(mcp: dict[str, Any] | None) -> str:
    if not mcp:
        return """# G. CMC MCP

Status: blocked pending the implementation session's tool inventory.

Keyless REST does not imply keyless MCP. This CLI does not read or send an MCP key. A written Cursor config is not a connection.
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

Existing policy excludes stablecoin bases and leveraged-token bases at the Binance contract filter. Wrapped assets are not merged into an underlying. A `1000` prefix is not stripped. Symbol collisions are excluded. Missing quote volume is insufficient evidence, not zero.

Owner monitoring is a separate count. This phase does not execute the production scanner, so scan attempts are zero and are not called successful scans.

Synthetic stage tests cover the boundary. A live stage count below is included only when an online summary is attached, and its `count_kind` says whether it is live or a synthetic transport.
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

Same-venue vendor comparison is not applicable unless a vendor payload identifies the venue and the sample summary says it was fetched. Cross-venue differences are context. Rank differences are methodology until a defect is shown.
"""
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
        "The offline microbenchmark times freshness checks only. It does not claim Runtime capacity.\n\n"
        "Cost projection uses 288 cycles per day only as a what-if for a 300-second cadence, "
        "and a 30-day month. Request counts are not billing credits. "
        "The code-default 60-second interval would be 1440 cycles per day if that default were the Runtime setting. "
        "Runtime cadence is unverified.\n"
    )
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
            f"- Head SHA: `{meta.get('head_sha', 'pending commit')}`",
            f"- PR: {meta.get('pr_url', 'pending')}",
            f"- Tests: {meta.get('tests', 'pending the full pytest run')}",
            f"- CI: {meta.get('ci', 'pending the pull request checks')}",
            "",
            "Production paths touched: none intended. The diff should be the research package, its CLI, tests, docs, and `.gitignore`.",
            "",
            "Independent reviewer: Astra. This document is not Astra approval.",
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
