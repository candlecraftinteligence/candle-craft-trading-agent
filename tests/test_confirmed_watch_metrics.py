from __future__ import annotations

from types import SimpleNamespace

from app.watch_iteration import telegram_outbox_status_summary


def test_watch_summary_distinguishes_confirmed_transitions_candidates_and_sends() -> None:
    summary = SimpleNamespace(
        sent=2,
        skipped=0,
        duplicate=0,
        blocked=2,
        blocked_repeat=0,
        failed=0,
        deliveries=(),
        confirmed_alert_audit=SimpleNamespace(
            confirmed_candidates_seen=3,
            confirmed_prefilter_passed=1,
            confirmed_policy_disabled=0,
            signal_confirmed_attempts_created=3,
            signal_confirmed_sent=1,
        ),
    )

    counts = telegram_outbox_status_summary(summary)

    assert counts["confirmed_transitions"] == 3
    assert counts["public_confirmed_candidates"] == 3
    assert counts["public_confirmed_prefilter_passed"] == 1
    assert counts["public_confirmed_rejected_pretransport"] == 2
    assert counts["public_confirmed_attempt_records"] == 3
    assert counts["public_confirmed_sent"] == 1
