from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.analytics.replay_dataset_coverage import (
    ReplayDatasetCoverageDimension,
    ReplayDatasetCoverageResult,
    analyze_replay_dataset_coverage,
)
from app.analytics.replay_dataset_export import export_replay_dataset_from_files
from app.analytics.replay_dataset_quality import (
    ReplayDatasetQualityIssue,
    ReplayDatasetQualityResult,
    analyze_replay_rows,
)
from app.analytics.replay_failure_taxonomy import (
    ReplayFailureTaxonomyDimension,
    ReplayFailureTaxonomyResult,
    analyze_replay_failure_taxonomy,
)

NA = "N/A"
REPLAY_RESEARCH_REPORT_SCHEMA_VERSION = "replay_research_report_v1"

REPLAY_RESEARCH_REPORT_SAFETY_NOTE = (
    "Replay research reporting is audit/research-only. It does not execute replay, create signals, "
    "place trades, call exchanges, send Telegram messages, mutate artifacts, update performance memory, "
    "alter database records, or change scanner, strategy, risk, scoring, lifecycle, portfolio, regime, "
    "alert, or setup gates."
)

IssueSeverity = Literal["info", "warning"]
SECRET_KEY_FRAGMENTS = ("secret", "token", "password", "private_key", "api_key", "api_secret")


@dataclass(frozen=True)
class ReplayResearchReportSection:
    title: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayResearchReportPriority:
    priority: str
    severity: IssueSeverity
    evidence: str
    suggested_next_phase: str
    safety_note: str


@dataclass(frozen=True)
class ReplayResearchReportSummary:
    schema_version: str = REPLAY_RESEARCH_REPORT_SCHEMA_VERSION
    source: str = "in_memory"
    generated_at_utc: str = NA
    artifact_count: int = 0
    total_rows: int = 0
    replay_ready_rows: int = 0
    replay_ready_rate: float = 0.0
    quality_score: float = 0.0
    symbol_count: int = 0
    sparse_symbol_count: int = 0
    failure_rows: int = 0
    failure_row_rate: float = 0.0
    top_failure_families: dict[str, int] = field(default_factory=dict)
    top_first_failed_gates: dict[str, int] = field(default_factory=dict)
    top_rejection_reasons: dict[str, int] = field(default_factory=dict)
    top_lifecycle_buckets: dict[str, int] = field(default_factory=dict)
    top_setup_research_buckets: dict[str, int] = field(default_factory=dict)
    warning_count: int = 0
    error_count: int = 0
    no_setup_rows: int = 0
    trade_idea_rows: int = 0
    alert_rows: int = 0
    journal_entry_rows: int = 0
    terminal_outcome_rows: int = 0
    rows_with_result_r: int = 0


@dataclass(frozen=True)
class ReplayResearchReportResult:
    source: str
    schema_version: str = REPLAY_RESEARCH_REPORT_SCHEMA_VERSION
    summary: ReplayResearchReportSummary = field(default_factory=ReplayResearchReportSummary)
    artifact_inputs: tuple[str, ...] = ()
    priorities: tuple[ReplayResearchReportPriority, ...] = ()
    sections: tuple[ReplayResearchReportSection, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    safety_notes: tuple[str, ...] = (REPLAY_RESEARCH_REPORT_SAFETY_NOTE,)


def default_replay_research_artifact_paths(project_root: Path | None = None) -> list[Path]:
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
    return [
        root / "scan_output.json",
        root / "scan_runs" / "latest_scan.json",
        root / "scan_runs" / "watch_state.json",
        root / "scan_runs" / "performance_memory.json",
    ]


def build_replay_research_report_from_artifacts(
    paths: list[Path],
    source: str = "local_artifacts",
    top_n: int = 10,
) -> ReplayResearchReportResult:
    normalized_paths = [Path(path) for path in paths]
    export_result = export_replay_dataset_from_files(normalized_paths)
    return _build_report(
        rows=list(export_result.rows),
        source=source,
        artifact_inputs=tuple(_redact_text(str(path)) for path in normalized_paths),
        artifact_count=len(normalized_paths),
        top_n=top_n,
        extra_warnings=_artifact_warning_messages(export_result.warnings),
        extra_warning_count=len(export_result.warnings),
        extra_errors=tuple(_redact_text(message) for message in export_result.errors),
        extra_error_count=len(export_result.errors),
    )


def build_replay_research_report_from_rows(
    rows: list[Any],
    source: str = "in_memory",
    top_n: int = 10,
) -> ReplayResearchReportResult:
    return _build_report(
        rows=list(rows),
        source=source,
        artifact_inputs=(),
        artifact_count=0,
        top_n=top_n,
        extra_warnings=(),
        extra_warning_count=0,
        extra_errors=(),
        extra_error_count=0,
    )


def replay_research_report_to_dict(result: ReplayResearchReportResult) -> dict[str, Any]:
    return _jsonable(asdict(result))


def format_replay_research_report_markdown(result: ReplayResearchReportResult) -> str:
    lines: list[str] = []
    for index, section in enumerate(result.sections):
        prefix = "#" if index == 0 else "##"
        lines.append(f"{prefix} {section.title}")
        lines.extend(section.lines)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_report(
    *,
    rows: list[Any],
    source: str,
    artifact_inputs: tuple[str, ...],
    artifact_count: int,
    top_n: int,
    extra_warnings: tuple[str, ...],
    extra_warning_count: int,
    extra_errors: tuple[str, ...],
    extra_error_count: int,
) -> ReplayResearchReportResult:
    top_n = max(1, int(top_n))
    safe_source = _redact_text(source)

    quality = analyze_replay_rows(rows, source=safe_source)
    coverage = analyze_replay_dataset_coverage(rows, source=safe_source, top_n=top_n)
    taxonomy = analyze_replay_failure_taxonomy(rows, source=safe_source, top_n=top_n)

    quality_warnings = _quality_messages(quality, "warning")
    coverage_warnings = _coverage_messages(coverage, "warning")
    taxonomy_warnings = tuple(_redact_text(message) for message in taxonomy.warnings)
    quality_errors = _quality_messages(quality, "error")
    coverage_errors = _coverage_messages(coverage, "error")
    taxonomy_errors = tuple(_redact_text(message) for message in taxonomy.errors)

    warnings = _unique_strings((*extra_warnings, *quality_warnings, *coverage_warnings, *taxonomy_warnings))
    errors = _unique_strings((*extra_errors, *quality_errors, *coverage_errors, *taxonomy_errors))
    warning_count = extra_warning_count + len(quality_warnings) + len(coverage_warnings) + len(taxonomy_warnings)
    error_count = extra_error_count + len(quality_errors) + len(coverage_errors) + len(taxonomy_errors)

    summary = _summary_from_components(
        source=safe_source,
        artifact_count=artifact_count,
        quality=quality,
        coverage=coverage,
        taxonomy=taxonomy,
        warning_count=warning_count,
        error_count=error_count,
        top_n=top_n,
    )
    priorities = tuple(_research_priorities(summary))
    sections = _sections(summary, artifact_inputs, priorities, warnings, errors)

    return ReplayResearchReportResult(
        source=safe_source,
        summary=summary,
        artifact_inputs=artifact_inputs,
        priorities=priorities,
        sections=sections,
        warnings=warnings,
        errors=errors,
    )


def _summary_from_components(
    *,
    source: str,
    artifact_count: int,
    quality: ReplayDatasetQualityResult,
    coverage: ReplayDatasetCoverageResult,
    taxonomy: ReplayFailureTaxonomyResult,
    warning_count: int,
    error_count: int,
    top_n: int,
) -> ReplayResearchReportSummary:
    quality_summary = quality.summary
    coverage_summary = coverage.summary
    taxonomy_summary = taxonomy.summary
    return ReplayResearchReportSummary(
        source=source,
        artifact_count=artifact_count,
        total_rows=quality_summary.total_rows,
        replay_ready_rows=quality_summary.replay_ready_rows,
        replay_ready_rate=quality_summary.replay_ready_rate,
        quality_score=quality_summary.quality_score,
        symbol_count=quality_summary.symbol_count,
        sparse_symbol_count=coverage_summary.sparse_symbol_count,
        failure_rows=taxonomy_summary.failure_rows,
        failure_row_rate=taxonomy_summary.failure_row_rate,
        top_failure_families=_failure_dimension_counts(taxonomy, "failure_family", top_n),
        top_first_failed_gates=_failure_dimension_counts(taxonomy, "first_failed_gate", top_n),
        top_rejection_reasons=_failure_dimension_counts(taxonomy, "rejection_reason", top_n),
        top_lifecycle_buckets=_coverage_dimension_counts(coverage, "lifecycle_bucket", top_n),
        top_setup_research_buckets=_coverage_dimension_counts(coverage, "setup_research_bucket", top_n),
        warning_count=warning_count,
        error_count=error_count,
        no_setup_rows=quality_summary.no_setup_rows,
        trade_idea_rows=quality_summary.trade_idea_rows,
        alert_rows=quality_summary.alert_rows,
        journal_entry_rows=quality_summary.journal_entry_rows,
        terminal_outcome_rows=quality_summary.terminal_outcome_rows,
        rows_with_result_r=quality_summary.rows_with_result_r,
    )


def _research_priorities(summary: ReplayResearchReportSummary) -> list[ReplayResearchReportPriority]:
    priorities: list[ReplayResearchReportPriority] = []
    total_rows = summary.total_rows
    failure_rows = summary.failure_rows
    no_setup_family_rows = summary.top_failure_families.get("no_setup", 0)
    no_setup_evidence_rows = max(summary.no_setup_rows, no_setup_family_rows)

    if total_rows == 0:
        priorities.append(
            ReplayResearchReportPriority(
                priority="Collect readable replay artifacts before drawing dataset conclusions.",
                severity="warning",
                evidence="total_rows=0",
                suggested_next_phase="Phase 44H replay artifact collection check",
                safety_note="Research only; do not invent missing rows, outcomes, or market data.",
            )
        )
        return priorities

    if no_setup_evidence_rows > 0 and (
        _rate(no_setup_evidence_rows, total_rows) >= 0.4
        or _top_key(summary.top_failure_families) == "no_setup"
    ):
        priorities.append(
            ReplayResearchReportPriority(
                priority="Investigate no-setup concentration before changing gates.",
                severity="warning",
                evidence=(
                    f"no_setup_rows={summary.no_setup_rows}, "
                    f"failure_family.no_setup={no_setup_family_rows}, total_rows={total_rows}"
                ),
                suggested_next_phase="Phase 44H no-setup concentration review",
                safety_note="Research only; rejected and no-setup rows are valid evidence, not trade signals.",
            )
        )

    readiness_gap_rows = summary.top_failure_families.get("replay_readiness_gap", 0)
    if readiness_gap_rows > 0 and (
        _rate(readiness_gap_rows, max(1, failure_rows)) >= 0.2
        or summary.replay_ready_rate < 0.8
    ):
        priorities.append(
            ReplayResearchReportPriority(
                priority="Improve replay identifiers/timestamps/outcome fields before expectancy claims.",
                severity="warning",
                evidence=(
                    f"failure_family.replay_readiness_gap={readiness_gap_rows}, "
                    f"replay_ready_rate={_percent(summary.replay_ready_rate)}"
                ),
                suggested_next_phase="Phase 44H replay readiness enrichment",
                safety_note="Research only; do not calculate profitability from incomplete replay rows.",
            )
        )

    pullback_rows = summary.top_failure_families.get("pullback_failure", 0)
    if pullback_rows > 0:
        priorities.append(
            ReplayResearchReportPriority(
                priority="Review pullback failure examples before any OB/FVG/fib/RR policy discussion.",
                severity="warning",
                evidence=f"failure_family.pullback_failure={pullback_rows}",
                suggested_next_phase="Phase 44H pullback failure sample review",
                safety_note="Research only; keep OB/FVG, fib, RR, and setup gates unchanged in this report.",
            )
        )

    if summary.total_rows >= 3 and summary.symbol_count > 0 and summary.sparse_symbol_count >= max(2, summary.symbol_count // 2):
        priorities.append(
            ReplayResearchReportPriority(
                priority="Gather longer-duration data before symbol-level conclusions.",
                severity="warning",
                evidence=f"sparse_symbol_count={summary.sparse_symbol_count}, symbol_count={summary.symbol_count}",
                suggested_next_phase="Phase 44H longer-duration replay data collection",
                safety_note="Research only; sparse symbol coverage is not a trading signal.",
            )
        )

    if summary.terminal_outcome_rows == 0 and summary.rows_with_result_r == 0:
        priorities.append(
            ReplayResearchReportPriority(
                priority="Outcome enrichment needed before expectancy analysis.",
                severity="warning",
                evidence="terminal_outcome_rows=0, rows_with_result_r=0",
                suggested_next_phase="Phase 44H terminal outcome/result_r enrichment",
                safety_note="Research only; do not infer outcomes or calculate fake profitability.",
            )
        )

    if summary.quality_score >= 80.0 and summary.warning_count > 0:
        priorities.append(
            ReplayResearchReportPriority(
                priority="Keep current export contract and reduce warnings incrementally.",
                severity="info",
                evidence=f"quality_score={summary.quality_score:.2f}, warning_count={summary.warning_count}",
                suggested_next_phase="Phase 44H warning reduction backlog",
                safety_note="Research only; preserve the existing replay export contract.",
            )
        )

    return priorities


def _sections(
    summary: ReplayResearchReportSummary,
    artifact_inputs: tuple[str, ...],
    priorities: tuple[ReplayResearchReportPriority, ...],
    warnings: tuple[str, ...],
    errors: tuple[str, ...],
) -> tuple[ReplayResearchReportSection, ...]:
    return (
        ReplayResearchReportSection(
            "Candle Craft Replay Research Report",
            (
                f"Schema version: {summary.schema_version}",
                f"Source: {summary.source}",
                f"Generated at UTC: {summary.generated_at_utc}",
            ),
        ),
        ReplayResearchReportSection(
            "Executive Summary",
            (
                f"- Artifacts: {summary.artifact_count}",
                f"- Rows: {summary.total_rows}",
                f"- Replay ready: {summary.replay_ready_rows} ({_percent(summary.replay_ready_rate)})",
                f"- Quality score: {summary.quality_score:.2f}/100",
                f"- Warnings/errors: {summary.warning_count} warning, {summary.error_count} error",
            ),
        ),
        ReplayResearchReportSection(
            "Artifact Inputs",
            _artifact_lines(artifact_inputs),
        ),
        ReplayResearchReportSection(
            "Dataset Quality",
            (
                f"- Symbols: {summary.symbol_count}",
                (
                    "- Row presence: "
                    f"no_setup={summary.no_setup_rows}, "
                    f"trade_idea={summary.trade_idea_rows}, "
                    f"alert={summary.alert_rows}, "
                    f"journal={summary.journal_entry_rows}"
                ),
                f"- Terminal outcome rows: {summary.terminal_outcome_rows}",
                f"- Rows with result_r: {summary.rows_with_result_r}",
            ),
        ),
        ReplayResearchReportSection(
            "Dataset Coverage",
            (
                f"- Sparse symbols: {summary.sparse_symbol_count}",
                f"- Top lifecycle buckets: {_format_counter(summary.top_lifecycle_buckets)}",
                f"- Top setup research buckets: {_format_counter(summary.top_setup_research_buckets)}",
            ),
        ),
        ReplayResearchReportSection(
            "Failure Taxonomy",
            (
                f"- Failure rows: {summary.failure_rows} ({_percent(summary.failure_row_rate)})",
                f"- Top failure families: {_format_counter(summary.top_failure_families)}",
                f"- Top first failed gates: {_format_counter(summary.top_first_failed_gates)}",
                f"- Top rejection reasons: {_format_counter(summary.top_rejection_reasons)}",
            ),
        ),
        ReplayResearchReportSection(
            "Replay Readiness Gaps",
            (
                f"- Replay ready rate: {_percent(summary.replay_ready_rate)}",
                f"- Warning count: {summary.warning_count}",
                f"- Error count: {summary.error_count}",
                f"- Top warnings: {_format_messages(warnings)}",
                f"- Top errors: {_format_messages(errors)}",
            ),
        ),
        ReplayResearchReportSection(
            "Research Priorities",
            _priority_lines(priorities),
        ),
        ReplayResearchReportSection(
            "Safety Notes",
            (
                f"- {REPLAY_RESEARCH_REPORT_SAFETY_NOTE}",
                "- Priorities are research priorities, not trading signals or profitability claims.",
                "- Missing values remain N/A; unreliable data remains Unverified.",
            ),
        ),
    )


def _artifact_lines(artifact_inputs: tuple[str, ...]) -> tuple[str, ...]:
    if not artifact_inputs:
        return ("- No artifact files supplied; report built from in-memory rows.",)
    return tuple(f"- {path}" for path in artifact_inputs)


def _priority_lines(priorities: tuple[ReplayResearchReportPriority, ...]) -> tuple[str, ...]:
    if not priorities:
        return ("- N/A",)
    lines: list[str] = []
    for priority in priorities:
        lines.append(f"- {priority.severity.upper()}: {priority.priority}")
        lines.append(f"  Evidence: {priority.evidence}")
        lines.append(f"  Suggested next phase: {priority.suggested_next_phase}")
        lines.append(f"  Safety: {priority.safety_note}")
    return tuple(lines)


def _quality_messages(result: ReplayDatasetQualityResult, severity: Literal["warning", "error"]) -> tuple[str, ...]:
    return tuple(_quality_message(issue) for issue in result.issues if issue.severity == severity)


def _quality_message(issue: ReplayDatasetQualityIssue) -> str:
    return _redact_text(f"{issue.code}: {issue.message} path={issue.path}")


def _coverage_messages(result: ReplayDatasetCoverageResult, severity: Literal["warning", "error"]) -> tuple[str, ...]:
    return tuple(
        _redact_text(f"{gap.code}: {gap.message} path={gap.path}")
        for gap in result.gaps
        if gap.severity == severity
    )


def _artifact_warning_messages(warnings: Sequence[str]) -> tuple[str, ...]:
    if not warnings:
        return ()
    return (
        (
            f"Replay export reported {len(warnings)} warning(s); "
            "row-level export warnings are summarized by count in this report."
        ),
    )


def _coverage_dimension_counts(result: ReplayDatasetCoverageResult, dimension_name: str, top_n: int) -> dict[str, int]:
    dimension = _coverage_dimension(result, dimension_name)
    if dimension is None:
        return {}
    return _sanitize_counts({bucket.key: bucket.count for bucket in dimension.top_buckets[:top_n]})


def _coverage_dimension(
    result: ReplayDatasetCoverageResult,
    dimension_name: str,
) -> ReplayDatasetCoverageDimension | None:
    for dimension in result.dimensions:
        if dimension.dimension_name == dimension_name:
            return dimension
    return None


def _failure_dimension_counts(result: ReplayFailureTaxonomyResult, dimension_name: str, top_n: int) -> dict[str, int]:
    dimension = _failure_dimension(result, dimension_name)
    if dimension is None:
        return {}
    return _sanitize_counts({bucket.key: bucket.count for bucket in dimension.top_buckets[:top_n]})


def _failure_dimension(
    result: ReplayFailureTaxonomyResult,
    dimension_name: str,
) -> ReplayFailureTaxonomyDimension | None:
    for dimension in result.dimensions:
        if dimension.dimension_name == dimension_name:
            return dimension
    return None


def _sanitize_counts(counts: Mapping[str, int]) -> dict[str, int]:
    sanitized: dict[str, int] = {}
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        safe_key = _redact_text(str(key))
        sanitized[safe_key] = sanitized.get(safe_key, 0) + int(count)
    return dict(sorted(sanitized.items(), key=lambda item: (-item[1], item[0])))


def _format_counter(counts: Mapping[str, int]) -> str:
    if not counts:
        return NA
    return ", ".join(f"{key}={count}" for key, count in counts.items())


def _format_messages(messages: Sequence[str], limit: int = 5) -> str:
    if not messages:
        return NA
    return "; ".join(_redact_text(message) for message in messages[:limit])


def _top_key(counts: Mapping[str, int]) -> str:
    if not counts:
        return NA
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _redact_text(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _redact_text(value: Any) -> str:
    text = NA if value is None else str(value)
    lowered = text.lower()
    if any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS):
        return "[REDACTED]"
    return text


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return str(value)
    return value


__all__ = [
    "REPLAY_RESEARCH_REPORT_SCHEMA_VERSION",
    "ReplayResearchReportPriority",
    "ReplayResearchReportResult",
    "ReplayResearchReportSection",
    "ReplayResearchReportSummary",
    "build_replay_research_report_from_artifacts",
    "build_replay_research_report_from_rows",
    "default_replay_research_artifact_paths",
    "format_replay_research_report_markdown",
    "replay_research_report_to_dict",
]
