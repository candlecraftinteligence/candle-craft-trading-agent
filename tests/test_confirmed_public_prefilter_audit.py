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
    assert len(attempts) == 2
    assert {attempt.telegram_status for attempt in attempts} == {"blocked", "sent"}
    assert sum(
        attempt.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        for attempt in attempts
    ) == 1


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
