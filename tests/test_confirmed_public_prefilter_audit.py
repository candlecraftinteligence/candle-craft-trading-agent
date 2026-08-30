from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
)
from app.analytics.setup_quality import SetupQualityGrade
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.service import observation_from_symbol_result
from app.scoring.opportunity_scoring import score_opportunity

from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _diagnostics,
    _run_result,
    _setup_quality_with_grade,
    _symbol,
    _trade_idea,
    _with_lifecycle_fields,
    run,
)
from test_triggered_confirmed_telegram_delivery import (
    _generated_entry_batch,
    _service,
    _triggered_confirmed_result,
)


def _confirmed_candidate(case: str):
    kwargs: dict[str, object] = {
        "setup_quality": _setup_quality_with_grade(
            SetupQualityGrade.B_PLUS,
            quality_score=78,
        )
    }
    if case == "rr":
        kwargs.update(
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.9")),
            trade_idea=_trade_idea(best_rr=Decimal("2.9")),
        )
    elif case == "grade":
        kwargs["setup_quality"] = _setup_quality_with_grade(
            SetupQualityGrade.B,
            quality_score=74,
        )
    elif case == "technical":
        kwargs["technical_score"] = Decimal("49")
    elif case == "opportunity":
        kwargs["trade_idea"] = _trade_idea(opportunity_score=Decimal("79"))
    elif case != "eligible":
        raise AssertionError(f"Unknown fixture case: {case}")
    return _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=f"confirmed-audit-{case}",
        **kwargs,
    )


def _confirmed_attempts(db_path: Path):
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        return tuple(
            attempt
            for attempt in repository.list_attempts()
            if attempt.attempted_alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        )


def _healthy_score_result(
    *,
    missing_data: tuple[str, ...] = (),
    unverified_data: tuple[str, ...] = (),
):
    return score_opportunity(
        {
            "technical_score": Decimal("100"),
            "derivatives_score": Decimal("100"),
            "risk_approved": True,
            "best_rr": Decimal("3"),
            "liquidity_score": Decimal("100"),
            "catalyst_score": Decimal("100"),
            "data_quality_score": Decimal("100"),
            "invalidation_present": True,
            "setup_location": "edge",
            "missing_data": missing_data,
            "unverified_data": unverified_data,
        }
    )


PRODUCTION_OPTIONAL_MISSING = (
    "liquidation_data: N/A",
    "liquidity_below: N/A",
    "liquidity_above: N/A",
    "orderflow_summary: N/A",
    "cvd: N/A",
    "btc_context: N/A",
    "btc_d_context: N/A",
    "event_risk_context: N/A",
    "weekend_filter: N/A",
    "sector_rotation: N/A",
    "narrative: N/A",
    "liquidation_heatmap: N/A",
)


def _with_data_health(
    symbol,
    *,
    missing_data: tuple[str, ...] = (),
    unverified_data: tuple[str, ...] = (),
    strategy_missing_data: tuple[str, ...] = (),
    strategy_unverified_data: tuple[str, ...] = (),
    derivatives_missing_data: tuple[str, ...] = (),
    derivatives_unverified_data: tuple[str, ...] = (),
    score_missing_data: tuple[str, ...] = (),
    score_unverified_data: tuple[str, ...] = (),
):
    diagnostics = dict(symbol.strategy_diagnostics["swing"])
    diagnostics.update(
        {
            "missing_data": strategy_missing_data,
            "unverified_data": strategy_unverified_data,
        }
    )
    return symbol.model_copy(
        update={
            "missing_data": missing_data,
            "unverified_data": unverified_data,
            "strategy_missing_data": strategy_missing_data,
            "strategy_unverified_data": strategy_unverified_data,
            "derivatives_missing_data": derivatives_missing_data,
            "derivatives_unverified_data": derivatives_unverified_data,
            "score_result": _healthy_score_result(
                missing_data=score_missing_data,
                unverified_data=score_unverified_data,
            ),
            "strategy_diagnostics": {"swing": diagnostics},
        }
    )


def test_eligible_confirmed_candidate_creates_attempt_and_sends(tmp_path: Path) -> None:
    db_path = tmp_path / "eligible-confirmed.db"
    sender = FakeSender()

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(_confirmed_candidate("eligible")),
            scan_run_id="eligible-confirmed",
        )
    )

    assert summary.sent == 1
    assert summary.confirmed_alert_audit.confirmed_candidates_seen == 1
    assert summary.confirmed_alert_audit.confirmed_prefilter_passed == 1
    assert summary.confirmed_alert_audit.signal_confirmed_attempts_created == 1
    assert summary.confirmed_alert_audit.signal_confirmed_sent == 1
    assert len(sender.messages) == 1
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert attempts[0].alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
    assert attempts[0].telegram_status == "sent"


def test_optional_enrichment_missing_passes_data_health_and_normal_gates_send(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "optional-enrichment-confirmed.db"
    sender = FakeSender()
    optional_missing = (
        "liquidation_heatmap: N/A",
        "cvd: N/A",
        "narrative: N/A",
        "btc_context: N/A",
    )
    symbol = _with_data_health(
        _confirmed_candidate("eligible"),
        missing_data=optional_missing,
        strategy_missing_data=optional_missing,
        score_missing_data=optional_missing,
    )

    observation = observation_from_symbol_result(symbol)
    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="optional-enrichment-confirmed",
        )
    )

    assert observation.data_health_failed is False
    assert observation.required_data_missing == ()
    assert observation.optional_data_missing == (
        "liquidation_heatmap",
        "cvd",
        "narrative",
        "btc_context",
    )
    assert summary.sent == 1
    assert summary.confirmed_alert_audit.confirmed_prefilter_passed == 1
    assert summary.confirmed_alert_audit.non_blocking_data_health_by_reason == {
        "optional_data_missing:liquidation_heatmap,cvd,narrative,btc_context": 1
    }
    assert len(sender.messages) == 1


def test_production_exact_optional_list_across_all_sources_does_not_hard_block(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "production-optional-confirmed.db"
    sender = FakeSender()
    unverified = ("CVD: Unverified", "liquidation_heatmap: Unverified")
    symbol = _with_data_health(
        _confirmed_candidate("eligible"),
        missing_data=PRODUCTION_OPTIONAL_MISSING[:4],
        unverified_data=unverified,
        strategy_missing_data=PRODUCTION_OPTIONAL_MISSING[4:10],
        derivatives_missing_data=PRODUCTION_OPTIONAL_MISSING[10:],
        derivatives_unverified_data=unverified,
        score_missing_data=PRODUCTION_OPTIONAL_MISSING,
        score_unverified_data=unverified,
    )

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="production-optional-confirmed",
        )
    )

    expected_fields = tuple(value.split(":", 1)[0] for value in PRODUCTION_OPTIONAL_MISSING)
    audit = summary.confirmed_alert_audit
    assert summary.sent == 1
    assert audit.confirmed_prefilter_passed == 1
    assert audit.blocked_before_attempt_by_reason == {}
    assert audit.non_blocking_data_health_by_reason == {
        f"optional_data_missing:{','.join(expected_fields)}": 1,
        "optional_data_unverified:cvd,liquidation_heatmap": 1,
    }
    assert len(sender.messages) == 1


def test_required_market_data_missing_hard_blocks_confirmed(tmp_path: Path) -> None:
    db_path = tmp_path / "required-missing-confirmed.db"
    sender = FakeSender()
    symbol = _with_data_health(
        _confirmed_candidate("eligible"),
        missing_data=("candles_15m: N/A",),
    )

    observation = observation_from_symbol_result(symbol)
    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="required-missing-confirmed",
        )
    )

    assert observation.data_health_failed is True
    assert observation.required_data_missing == ("candles_15m",)
    assert summary.sent == 0
    assert summary.blocked == 1
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason == {
        "required_data_missing": 1
    }
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert "required_data_missing:candles_15m" in attempts[0].blocked_reason
    assert sender.messages == []


def test_required_market_data_unverified_hard_blocks_confirmed(tmp_path: Path) -> None:
    db_path = tmp_path / "required-unverified-confirmed.db"
    sender = FakeSender()
    symbol = _with_data_health(
        _confirmed_candidate("eligible"),
        unverified_data=("technical: Unverified",),
    )

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="required-unverified-confirmed",
        )
    )

    assert summary.sent == 0
    assert summary.blocked == 1
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason == {
        "required_data_unverified": 1
    }
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert "required_data_unverified:technical" in attempts[0].blocked_reason
    assert sender.messages == []


def test_optional_unverified_is_retained_without_automatic_hard_block(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "optional-unverified-confirmed.db"
    sender = FakeSender()
    symbol = _with_data_health(
        _confirmed_candidate("eligible"),
        unverified_data=("CVD: Unverified", "liquidation_heatmap: Unverified"),
    )

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="optional-unverified-confirmed",
        )
    )

    assert summary.sent == 1
    assert summary.confirmed_alert_audit.non_blocking_data_health_by_reason == {
        "optional_data_unverified:cvd,liquidation_heatmap": 1
    }
    assert len(sender.messages) == 1


def test_optional_missing_with_low_technical_score_is_not_data_health_blocked(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "optional-low-technical-confirmed.db"
    sender = FakeSender()
    symbol = _with_data_health(
        _confirmed_candidate("technical"),
        missing_data=("cvd: N/A", "liquidation_heatmap: N/A", "narrative: N/A"),
    )

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="optional-low-technical-confirmed",
        )
    )

    assert summary.sent == 0
    assert summary.blocked == 1
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason == {
        "technical_score_below_min": 1
    }
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert "technical_score_below_min:49<50" in attempts[0].blocked_reason
    assert "required_data_" not in attempts[0].blocked_reason
    assert "data_health_failed" not in attempts[0].blocked_reason
    assert sender.messages == []


def test_required_missing_blocks_otherwise_excellent_confirmed_candidate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "required-excellent-confirmed.db"
    sender = FakeSender()
    excellent = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="confirmed-required-excellent",
        technical_score=Decimal("100"),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.A,
            quality_score=99,
        ),
        trade_idea=_trade_idea(
            opportunity_score=Decimal("99"),
            opportunity_grade="A+",
            opportunity_decision="high_quality_candidate",
        ),
    )
    symbol = _with_data_health(
        excellent,
        strategy_missing_data=("candles_5m: N/A",),
    )

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="required-excellent-confirmed",
        )
    )

    assert summary.sent == 0
    assert summary.blocked == 1
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert "required_data_missing:candles_5m" in attempts[0].blocked_reason
    assert sender.messages == []


@pytest.mark.parametrize(
    ("case", "reason_fragment", "reason_bucket"),
    (
        ("rr", "planned_rr_below_min:2.9<3", "rr_below_min"),
        ("grade", "confirmed_grade_below_min", "confirmed_grade_below_min"),
        ("technical", "technical_score_below_min:49<50", "technical_score_below_min"),
        ("opportunity", "opportunity_score_below_min:79<80", "opportunity_score_below_min"),
    ),
)
def test_ineligible_confirmed_candidate_persists_exact_prefilter_reason(
    tmp_path: Path,
    case: str,
    reason_fragment: str,
    reason_bucket: str,
) -> None:
    db_path = tmp_path / f"ineligible-confirmed-{case}.db"
    sender = FakeSender()

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(_confirmed_candidate(case)),
            scan_run_id=f"ineligible-confirmed-{case}",
        )
    )

    assert summary.sent == 0
    assert summary.blocked == 1
    assert summary.confirmed_alert_audit.confirmed_candidates_seen == 1
    assert summary.confirmed_alert_audit.confirmed_prefilter_passed == 0
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason[reason_bucket] == 1
    assert summary.confirmed_alert_audit.signal_confirmed_attempts_created == 1
    assert summary.confirmed_alert_audit.signal_confirmed_sent == 0
    assert sender.messages == []
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.telegram_status == "blocked"
    assert attempt.sent_at is None
    assert attempt.alert_type.startswith("SIGNAL_CONFIRMED_BLOCKED_")
    assert reason_fragment in attempt.blocked_reason
    assert attempt.scan_run_id == f"ineligible-confirmed-{case}"


def test_same_scan_triggered_send_is_independent_of_confirmed_prefilter_rejection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "same-scan-confirmed-blocked.db"
    symbol = _triggered_confirmed_result(_generated_entry_batch(db_path)).model_copy(
        update={"technical_score": Decimal("49")}
    )
    sender = FakeSender()

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="same-scan-confirmed-blocked",
        )
    )

    assert summary.sent == 1
    assert summary.blocked == 1
    assert len(sender.messages) == 1
    assert "TRIGGERED" in sender.messages[0]
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert attempts[0].telegram_status == "blocked"
    assert "technical_score_below_min:49<50" in attempts[0].blocked_reason
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        triggered = tuple(
            attempt
            for attempt in repository.list_attempts()
            if attempt.alert_type == TelegramAlertType.SETUP_TRIGGERED.value
        )
    assert len(triggered) == 1
    assert triggered[0].telegram_status == "sent"


def test_blocked_confirmed_audit_does_not_consume_real_dedupe_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "blocked-then-eligible.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    blocked = _confirmed_candidate("technical")
    eligible = blocked.model_copy(update={"technical_score": Decimal("70")})

    first = run(service.deliver_for_run(_run_result(blocked), scan_run_id="blocked-first"))
    second = run(service.deliver_for_run(_run_result(eligible), scan_run_id="eligible-second"))
    repeated = run(service.deliver_for_run(_run_result(eligible), scan_run_id="eligible-repeat"))

    assert first.blocked == 1
    assert second.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 3
    assert {attempt.telegram_status for attempt in attempts} == {"blocked", "sent"}
    assert sum(
        attempt.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        for attempt in attempts
    ) == 1
    blocked_reasons = {attempt.blocked_reason for attempt in attempts if attempt.telegram_status == "blocked"}
    assert any("technical_score_below_min" in reason for reason in blocked_reasons)
    assert "duplicate_equivalent_public_setup" in blocked_reasons


def test_invalid_confirmed_stored_plan_geometry_is_auditable(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid-confirmed-geometry.db"
    symbol = _with_lifecycle_fields(
        _confirmed_candidate("eligible"),
        stop_loss="105",
    )
    sender = FakeSender()

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="invalid-confirmed-geometry",
        )
    )

    assert summary.sent == 0
    assert summary.blocked == 1
    assert sender.messages == []
    attempts = _confirmed_attempts(db_path)
    assert len(attempts) == 1
    assert attempts[0].telegram_status == "blocked"
    assert attempts[0].blocked_reason.startswith(
        "blocked:invalid_stored_plan_geometry:"
    )


@pytest.mark.parametrize(
    ("diagnostic", "channel"),
    (
        (
            "microstructure_flow: N/A "
            "(status=UNAVAILABLE, reason=insufficient_window_coverage)",
            "missing",
        ),
        (
            "microstructure_flow: N/A (status=UNAVAILABLE, reason=stream_disconnected)",
            "missing",
        ),
        (
            "microstructure_flow: N/A "
            "(status=UNAVAILABLE, reason=subscription_limit_exceeded:max_symbols=100)",
            "missing",
        ),
        (
            "microstructure_flow: N/A (status=ERROR, reason=service_error:RuntimeError)",
            "missing",
        ),
        (
            "microstructure_flow: Unverified "
            "(status=STALE, reason=last_valid_event_stale)",
            "unverified",
        ),
    ),
)
def test_research_only_microstructure_matches_disabled_confirmed_delivery_eligibility(
    tmp_path: Path,
    diagnostic: str,
    channel: str,
) -> None:
    baseline_sender = FakeSender()
    baseline_symbol = _confirmed_candidate("eligible")
    baseline = run(
        _service(tmp_path / f"baseline-{channel}.db", baseline_sender).deliver_for_run(
            _run_result(baseline_symbol),
            scan_run_id=f"baseline-{channel}",
        )
    )

    flow_sender = FakeSender()
    health_values = (
        {"missing_data": (diagnostic,)}
        if channel == "missing"
        else {"unverified_data": (diagnostic,)}
    )
    flow_symbol = _with_data_health(
        _confirmed_candidate("eligible"),
        **health_values,
    )
    observation = observation_from_symbol_result(flow_symbol)
    flow = run(
        _service(tmp_path / f"flow-{channel}-{len(diagnostic)}.db", flow_sender).deliver_for_run(
            _run_result(flow_symbol),
            scan_run_id=f"flow-{channel}-{len(diagnostic)}",
        )
    )

    assert observation.data_health_failed is False
    assert observation.required_data_missing == ()
    assert observation.required_data_unverified == ()
    if channel == "missing":
        assert observation.optional_data_missing == ("microstructure_flow",)
        assert observation.optional_data_unverified == ()
    else:
        assert observation.optional_data_missing == ()
        assert observation.optional_data_unverified == ("microstructure_flow",)
    assert flow.sent == baseline.sent == 1
    assert flow.blocked == baseline.blocked == 0
    assert flow.confirmed_alert_audit.confirmed_prefilter_passed == 1
    assert flow.confirmed_alert_audit.blocked_before_attempt_by_reason == {}
    assert len(flow_sender.messages) == len(baseline_sender.messages) == 1
