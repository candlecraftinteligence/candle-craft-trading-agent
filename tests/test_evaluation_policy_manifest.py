"""EVAL_POLICY: unbound evaluation-policy manifest proofs.

These fixtures are synthetic. They do not read live or existing scan databases.
They do not mint admission, episode, trade, or policy IDs.
Evidence labels below are test documentation, not application enums.
"""

from __future__ import annotations

import ast
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.evaluation_policy import (
    ABSENT_EXTERNAL_CONTROLS,
    BINDING_CALLER_DECLARED_UNBOUND,
    BINDING_EXTERNALLY_CONTROLLED,
    BINDING_HARDCODED_UNBOUND,
    BINDING_IMPLEMENTED_UNBOUND,
    BINDING_NOT_A_POLICY_BINDING,
    FORBIDDEN_SURROGATES,
    MANIFEST_VERSION,
    REQUIRED_DIMENSIONS,
    classify_policy_surrogate,
    declared_replay_experiment_envelope,
    dumps_evaluation_policy_manifest,
    evaluation_policy_manifest,
    implemented_runtime_policy_snapshot,
    required_dimension_names,
)
from app.analytics.outcome_ownership import project_outcome_ownership
from app.backtesting.strategy_replay import ReplayConfig, _price_touched
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.outcome_policy import StoredPlanGeometry, entry_touched
from app.lifecycle.outcomes import OUTCOME_ELIGIBLE_STATES, ENTRY_CAUSALITY_CONTRACT
from app.lifecycle.state_machine import PLAN_LOCK_STATES
from app.storage.database import SCHEMA_VERSION, open_initialized_database

EVIDENCE_SUPPLIED_STATE = "direct evaluator / import-supported supplied state"
EVIDENCE_SOURCE_ONLY = "source inspection only, not executed here"
EVIDENCE_MALFORMED = "malformed / adversarial supplied state"

IDENTITY_KEYS = (
    "evaluation_policy_id",
    "policy_hash",
    "admission_id",
    "evaluation_episode_id",
)


def _walk_identity_keys(payload: object) -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in IDENTITY_KEYS:
                found.append((key, value))
            found.extend(_walk_identity_keys(value))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found.extend(_walk_identity_keys(item))
    return found


def test_manifest_is_deterministic_complete_and_unbound() -> None:
    payload = evaluation_policy_manifest()
    encoded = dumps_evaluation_policy_manifest()
    assert encoded == dumps_evaluation_policy_manifest()
    loaded = json.loads(encoded)
    assert loaded["manifest_version"] == MANIFEST_VERSION
    assert loaded == json.loads(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )
    assert tuple(loaded["required_dimensions"]) == REQUIRED_DIMENSIONS == required_dimension_names()
    assert set(loaded["dimensions"]) == set(REQUIRED_DIMENSIONS)
    assert loaded["completeness"]["complete_immutable_evaluation_policy_binding"] is False
    assert loaded["completeness"]["runtime_complete"] is False
    assert loaded["completeness"]["replay_complete"] is False
    assert loaded["completeness"]["hashing_source_completes_checklist"] is False
    assert loaded["completeness"]["current_code_is_historical_default"] is False
    assert loaded["completeness"]["missing_required_bindings"] == list(REQUIRED_DIMENSIONS)
    assert loaded["authority"]["replay_is_runtime_outcome_authority"] is False
    assert loaded["authority"]["prospective_admission_persistence"] == "STOP"
    assert loaded["authority"]["same_opportunity_producer_parity"] is False
    for key, value in _walk_identity_keys(loaded):
        assert value == UNAVAILABLE, key
    for name in ABSENT_EXTERNAL_CONTROLS:
        control = loaded["absent_external_controls"][name]
        assert control["binding_status"] == BINDING_EXTERNALLY_CONTROLLED
        assert control["value"] == NA
    for name in FORBIDDEN_SURROGATES:
        surrogate = loaded["forbidden_surrogates"][name]
        assert surrogate["binding_status"] == BINDING_NOT_A_POLICY_BINDING
        assert surrogate["evaluation_policy_id"] == UNAVAILABLE


def test_schema_remains_v25_without_policy_or_admission_binding(tmp_path: Path) -> None:
    path = tmp_path / "eval-policy-schema.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        progress_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        analytics_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)")
        }
        scan_cols = {row[1] for row in connection.execute("PRAGMA table_info(scan_runs)")}
        table_names = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert user_version == SCHEMA_VERSION == 25
    assert evaluation_policy_manifest()["schema_version_expected"] == SCHEMA_VERSION
    for forbidden in IDENTITY_KEYS + ("source_namespace",):
        assert forbidden not in progress_cols
        assert forbidden not in analytics_cols
        assert forbidden not in scan_cols
    assert "admitted_evaluation_episodes" not in table_names
    assert "evaluation_policies" not in table_names
    assert "raw_payload_format" in scan_cols
    format_binding = evaluation_policy_manifest()["local_markers_are_not_complete_bindings"][
        "storage_raw_payload_format"
    ]
    assert format_binding["binding_status"] == BINDING_NOT_A_POLICY_BINDING


def test_manifest_does_not_open_a_database_or_write_rows(tmp_path: Path) -> None:
    path = tmp_path / "untouched.sqlite"
    evaluation_policy_manifest()
    dumps_evaluation_policy_manifest()
    classify_policy_surrogate("scan_run_id", "ghost-run")
    declared_replay_experiment_envelope(ReplayConfig())
    implemented_runtime_policy_snapshot()
    assert not path.exists()


def test_no_policy_mint_or_bind_helpers_exist() -> None:
    import app.analytics.evaluation_policy as module

    for forbidden in (
        "mint_evaluation_policy_id",
        "mint_policy_hash",
        "bind_policy",
        "persist_evaluation_policy",
        "hash_source_as_policy",
    ):
        assert not hasattr(module, forbidden)


def test_source_hash_and_other_surrogates_are_not_policy_ids() -> None:
    digest = hashlib.sha256(Path("app/lifecycle/outcomes.py").read_bytes()).hexdigest()
    hashed = classify_policy_surrogate("source_sha256", digest)
    assert hashed["is_evaluation_policy_binding"] is False
    assert hashed["complete_immutable_binding"] is False
    assert hashed["evaluation_policy_id"] == UNAVAILABLE
    assert hashed["policy_hash"] == UNAVAILABLE
    assert hashed["value_supplied"] is True
    assert hashed["value_used_as_policy_id"] is False
    assert hashed["reason"] == "surrogate_is_not_evaluation_policy_binding"
    assert digest not in json.dumps(hashed, default=str)

    known = (
        "pinned_git_sha",
        "schema_version",
        "scan_run_id",
        "database_filename",
        "script_name",
        "algorithm_marker",
        "document_revision",
        "fixture_label",
        "raw_payload_format",
        "plan_version_id",
        "lifecycle_id",
    )
    for kind in known:
        result = classify_policy_surrogate(kind, "not-a-policy")
        assert result["evaluation_policy_id"] == UNAVAILABLE
        assert result["is_evaluation_policy_binding"] is False

    unknown = classify_policy_surrogate("complete_binding", {"pretend": True})
    assert unknown["reason"] == "unknown_surrogate_is_not_evaluation_policy_binding"
    assert unknown["evaluation_policy_id"] == UNAVAILABLE
    empty = classify_policy_surrogate("")
    assert empty["kind"] == NA
    assert empty["is_evaluation_policy_binding"] is False
    _ = EVIDENCE_MALFORMED


def test_zone_overlap_without_point_touch_remains_a_named_policy_difference() -> None:
    geometry = StoredPlanGeometry(
        direction="long",
        entry_low=Decimal("100"),
        entry_high=Decimal("102"),
        stop_loss=Decimal("90"),
        targets=(Decimal("110"), Decimal("120"), Decimal("130")),
    )
    high = Decimal("100.5")
    low = Decimal("99.5")
    entry = Decimal("101")
    candle = SimpleNamespace(low=low, high=high)
    assert entry_touched(high, low, geometry) is True
    assert _price_touched(candle, entry) is False
    dimension = evaluation_policy_manifest()["dimensions"]["entry_model"]
    assert dimension["runtime"]["implemented"] == "zone_overlap"
    assert dimension["replay"]["implemented"] == "point_touch"
    assert dimension["runtime"]["binding_status"] == BINDING_IMPLEMENTED_UNBOUND
    assert dimension["replay"]["binding_status"] == BINDING_HARDCODED_UNBOUND
    assert dimension["comparable_without_named_assumption"] is False
    assert dimension["complete_immutable_binding"] is False
    _ = EVIDENCE_SUPPLIED_STATE


def test_replay_config_difference_is_declared_envelope_not_a_policy_id() -> None:
    conservative = declared_replay_experiment_envelope(ReplayConfig())
    optimistic = declared_replay_experiment_envelope(
        ReplayConfig(same_candle_policy="optimistic", max_hold_candles=3)
    )
    assert conservative["complete_immutable_binding"] is False
    assert optimistic["complete_immutable_binding"] is False
    assert conservative["evaluation_policy_id"] == UNAVAILABLE
    assert optimistic["evaluation_policy_id"] == UNAVAILABLE
    assert conservative["declared"]["same_candle_policy"] == "conservative"
    assert optimistic["declared"]["same_candle_policy"] == "optimistic"
    assert conservative["declared"]["max_hold_candles"] == "unset_uses_mode_timeframe_default"
    assert optimistic["declared"]["max_hold_candles"] == 3
    assert conservative["declared"] != optimistic["declared"]
    assert "entry_model" in conservative["still_hardcoded"]
    timeframe = declared_replay_experiment_envelope({"execution_timeframe": "5m", "same_candle_policy": "conservative"})
    assert timeframe["declared"]["execution_timeframe"] == "5m"
    assert timeframe["is_evaluation_policy_binding"] is False
    dimension = evaluation_policy_manifest()["dimensions"]["same_candle_precedence"]
    assert dimension["replay"]["binding_status"] == BINDING_CALLER_DECLARED_UNBOUND


def test_runtime_snapshot_is_not_a_versioned_binding() -> None:
    snapshot = implemented_runtime_policy_snapshot()
    assert snapshot["complete_immutable_binding"] is False
    assert snapshot["durable_policy_object"] is False
    assert snapshot["evaluation_policy_id"] == UNAVAILABLE
    assert set(snapshot["implemented"]) == set(REQUIRED_DIMENSIONS)
    assert snapshot["implemented"]["entry_model"] == "zone_overlap"
    assert snapshot["implemented"]["same_candle_precedence"] == "conservative_stop_wins"
    assert snapshot["implemented"]["expiry_horizon"] == "no_evaluator_hold_window"


def test_source_predicates_match_named_manifest_dimensions() -> None:
    outcome_policy = Path("app/lifecycle/outcome_policy.py").read_text(encoding="utf-8")
    outcomes = Path("app/lifecycle/outcomes.py").read_text(encoding="utf-8")
    replay = Path("app/backtesting/strategy_replay.py").read_text(encoding="utf-8")
    stop = Path("app/lifecycle/outcome_events.py").read_text(encoding="utf-8")
    manifest = evaluation_policy_manifest()

    assert "high >= geometry.entry_low and low <= geometry.entry_high" in outcome_policy
    simulate = replay.split("def _simulate_trade(", 1)[1].split("\ndef _evaluate_exit_candle", 1)[0]
    assert "candidate.entry_low" not in simulate
    assert "candidate.entry_high" not in simulate
    assert "_price_touched(candle, candidate.entry)" in simulate
    assert "range(candidate.detected_at_index + 1, max_fill_index + 1)" in simulate
    assert '"ambiguity_policy": "conservative_stop_wins"' in outcomes
    assert ENTRY_CAUSALITY_CONTRACT == "fully_post_boundary_closed_candle_v1"
    assert ENTRY_CAUSALITY_CONTRACT in outcomes
    assert "entry_and_stop_same_candle_stop_wins" in outcomes
    assert "post_entry_stop_and_target_same_candle_stop_wins" in outcomes
    assert 'update={f"tp{target_number}_at": candle_close}' in outcomes
    assert '"tp1_at"' not in stop.split("def record_stop(", 1)[1].split("def record_terminal_transition", 1)[0]
    assert replay.count("_evaluate_exit_candle") == 1
    tree = ast.parse(replay)
    called = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_evaluate_exit_candle"
    ]
    assert called == []
    assert manifest["dimensions"]["same_candle_precedence"]["replay"]["unused_helper"].endswith(
        "_evaluate_exit_candle"
    )
    _ = EVIDENCE_SOURCE_ONLY


def test_production_python_does_not_persist_policy_ids() -> None:
    hits: list[str] = []
    for path in (*Path("app").rglob("*.py"), *Path("scripts").rglob("*.py")):
        if path.name == "evaluation_policy.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "evaluation_policy_id" in text or "policy_hash" in text:
            hits.append(path.as_posix())
    assert hits == []
    production_hits = []
    for path in (*Path("app").rglob("*.py"), *Path("scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "evaluate_closed_candle_outcomes(" in text and path.name != "outcomes.py":
            production_hits.append(path.as_posix())
    assert production_hits == ["app/lifecycle/service.py"]


def test_claim_preservation_admission_and_replay_authority_remain_unproven() -> None:
    payload = evidence_contract_payload()
    assert payload["unique_trade_count"]["status"] == UNAVAILABLE
    assert SetupLifecycleState.TRIGGERED not in PLAN_LOCK_STATES
    assert SetupLifecycleState.TRIGGERED in OUTCOME_ELIGIBLE_STATES
    assert SetupLifecycleState.WATCHLISTED in PLAN_LOCK_STATES
    ownership = project_outcome_ownership(
        lifecycle_records=[],
        progress_rows=[],
        provenance={"synthetic": True, "label": "eval-policy-empty"},
    )
    assert ownership["canonical_outcome_per_plan_version"]["established"] is False
    manifest = evaluation_policy_manifest()
    assert manifest["authority"]["canonical_episode_outcome"] == UNAVAILABLE
    assert manifest["authority"]["unique_trade_count"] == UNAVAILABLE
    assert manifest["authority"]["expectancy"] == UNAVAILABLE
    assert ReplayConfig().same_candle_policy == "conservative"
    _ = EVIDENCE_SOURCE_ONLY


def test_chatgpt_astra_return_report_is_copy_paste_complete() -> None:
    report = Path("docs/research/CCI_EVAL_POLICY_CHATGPT_ASTRA_REPORT.md").read_text(encoding="utf-8")
    assert "BEGIN ASTRA REPORT" in report
    assert "END ASTRA REPORT" in report
    assert report.index("BEGIN ASTRA REPORT") < report.index("END ASTRA REPORT")
    required = (
        "Admission STOP remains",
        "SCHEMA_VERSION",
        "evaluation_policy_id",
        "policy_hash",
        "unavailable",
        "replay as runtime outcome authority",
        "CORRECTNESS > COMPLETION",
        "NOT authorized",
        "unique_trade_count",
        "https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/117",
    )
    for token in required:
        assert token in report, token
        body = report.split("\nBEGIN ASTRA REPORT\n", 1)[1].split("\nEND ASTRA REPORT", 1)[0]
        assert "Do not implement it from this" in body
        assert "The original master-prompt file body was not available to Cursor." in body
