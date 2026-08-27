from __future__ import annotations

import pytest

from app.core.confirmed_data_health import classify_confirmed_data_health


@pytest.mark.parametrize(
    "diagnostic",
    [
        "microstructure_flow: N/A (status=UNAVAILABLE, reason=insufficient_window_coverage)",
        "microstructure_flow: N/A (status=UNAVAILABLE, reason=stream_disconnected)",
        (
            "microstructure_flow: N/A "
            "(status=UNAVAILABLE, reason=subscription_limit_exceeded:max_symbols=100)"
        ),
        "microstructure_flow: N/A (status=ERROR, reason=service_error:RuntimeError)",
        "microstructure_flow: N/A (status=UNAVAILABLE, reason=service_not_running)",
    ],
)
def test_unavailable_microstructure_is_explicit_optional_missing(diagnostic: str) -> None:
    report = classify_confirmed_data_health(missing_values=((diagnostic,),))

    assert report.blocked is False
    assert report.required_missing == ()
    assert report.optional_missing == ("microstructure_flow",)
    assert report.blocking_reasons == ()
    assert report.diagnostic_reasons == (
        "optional_data_missing:microstructure_flow",
    )


def test_stale_or_gap_microstructure_is_explicit_optional_unverified() -> None:
    report = classify_confirmed_data_health(
        unverified_values=(
            (
                "microstructure_flow: Unverified "
                "(status=STALE, reason=last_valid_event_stale)",
                "microstructure_flow: Unverified "
                "(status=UNAVAILABLE, reason=aggregate_trade_id_gap_in_window)",
            ),
        ),
    )

    assert report.blocked is False
    assert report.required_unverified == ()
    assert report.optional_unverified == ("microstructure_flow",)
    assert report.diagnostic_reasons == (
        "optional_data_unverified:microstructure_flow",
    )


def test_unknown_future_data_health_field_still_fails_closed() -> None:
    report = classify_confirmed_data_health(
        missing_values=(("future_unknown_field: N/A",),),
    )

    assert report.blocked is True
    assert report.required_missing == ("future_unknown_field",)
    assert report.optional_missing == ()
    assert report.blocking_reasons == (
        "required_data_missing:future_unknown_field",
    )
