from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    FOOTER,
    HEADER_PREFIX,
    SignalEdgeEvidence,
    TelegramAlertType,
    SignalMessageContext,
    TelegramSignalMessage,
    format_public_no_trade_message,
    format_telegram_price,
    format_telegram_rr,
    format_telegram_signal_message,
)


def _edge(**overrides: object) -> SignalEdgeEvidence:
    data = {
        "sweep_present": True,
        "sweep_direction": "bullish",
        "swept_level": Decimal("99"),
        "sweep_wick": Decimal("98.5"),
        "structure_present": True,
        "structure_kind": "CHoCH",
        "structure_direction": "bullish",
        "structure_timeframe": "15m",
        "structure_level": Decimal("103"),
        "structure_close": Decimal("104"),
        "selected_zone_type": "OB_FVG_OVERLAP",
        "fib_aligned": True,
        "pullback_depth_ratio": Decimal("0.58"),
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "rr_to_tp2": Decimal("2.91918017"),
    }
    data.update(overrides)
    return SignalEdgeEvidence(**data)


def _message(**overrides: object) -> TelegramSignalMessage:
    data = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "signal_id": "sig-001",
        "mode": "scalp",
        "quality": "A",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "planned_rr": Decimal("2.91918017"),
        "structure_reason": "Sweep and reclaim into valid pullback.",
        "confirmation_needed": "15m BOS/CHoCH.",
        "confirmation_timeframe": "15m",
        "needs_next": ("Price must trade into the Limit Zone.",),
        "invalidation_reason": "Invalid if price accepts below 95.",
        "confluence": "LTF BOS/CHoCH confirmed.",
        "edge_evidence": _edge(),
    }
    data.update(overrides)
    return TelegramSignalMessage(**data)


def test_all_public_signal_messages_end_with_candle_craft_footer() -> None:
    for alert_type in TelegramAlertType:
        text = format_telegram_signal_message(alert_type, _message())

        assert text.startswith((HEADER_PREFIX, "🐺", "👁", "✅", "🔥", "🔴"))
        assert text.endswith(FOOTER)


def test_valid_scalp_signal_renders_premium_compact_card() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert text.startswith("🐺 BTCUSDT · LONG · SCALP")
    assert "🟢 SIGNAL CONFIRMED" in text
    assert "A · Score N/A · RR 2.92R" in text
    assert "Confirmation criteria satisfied." not in text
    assert "🎯 TRADE MAP" in text
    assert "Entry: 100 – 102" in text
    assert "SL: 95" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: 120" in text
    assert "🧠 INTELLIGENCE" in text
    assert "Price swept downside liquidity at 99 with a wick to 98.5, then closed back above the level." in text
    assert "15m bullish CHoCH closed above 103 at 104." in text
    assert "Entry 100 – 102 overlaps the selected OB/FVG overlap and the validated fib pullback zone" in text
    assert "The stored plan provides 2.92R to TP2." in text
    assert "⚔️ EXECUTION" in text
    assert "No chase outside the mapped zone." in text
    assert "Invalid if price accepts below 95." in text
    assert "🐺 Liquidity taken. Structure confirmed. Hunt active." in text
    assert "Not financial advice." in text
    assert "Actionability" + ":" not in text
    assert "Why this setup " + "matters" not in text
    assert "Execution " + "notes" not in text
    assert "What we want " + "next" not in text
    assert "Signal ID" not in text


def test_signal_formatter_uses_explicit_m5_confirmation_override() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(confirmation_timeframe="5m", edge_evidence=_edge(structure_timeframe="5m")),
    )

    assert "5m bullish CHoCH" in text
    assert "15m bullish CHoCH" not in text

def test_public_watchlist_formatter_matches_compact_watch_shape() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message())

    assert text.startswith("🐺 BTCUSDT · LONG · SCALP")
    assert "👁 ON THE RADAR — CONFIRMATION REQUIRED" in text
    assert "A · Score N/A · RR 2.92R" in text
    assert "🎯 TRADE MAP" in text
    assert "Entry: 100 – 102" in text
    assert "SL: 95" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: 120" in text
    assert "⚔️ EXECUTION" in text
    assert "Wait for confirmation.\nNo entry until structure accepts back through the trigger zone." in text
    assert "Invalid if price accepts below 95." in text
    assert "🐺 On the radar. Patience protects the edge." in text
    assert "Candle Craft | Signal. Structure. Execution." in text
    assert "Actionability" + ":" not in text
    assert "Why this setup " + "matters" not in text
    assert "Execution " + "notes" not in text
    assert "What we want " + "next" not in text
    assert "Potential RR" not in text
    assert "No confirmation = no trade." not in text
    assert "CONFIRMED" not in text


def test_public_watchlist_target_caution_renders_single_tp1_priority_warning() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            actionability_state="A_GRADE_ACTIONABLE_TARGET_CAUTION",
            target_failure_severity="target_caution_actionable",
            target_warning_reason="TP2 remains inside recent chop/range.",
        ),
    )

    assert "🟢 SIGNAL CONFIRMED · TP1 PRIORITY" in text
    assert "No chase outside the mapped zone." in text
    assert "TP1 reaction matters because TP2/TP3 path is choppy." in text
    assert text.count("TP1 reaction matters because TP2/TP3 path is choppy.") == 1
    assert "Actionability" + ":" not in text
    assert "TARGET " + "CAUTION" not in text
    assert "Target path is choppy/tighter" not in text
    assert "Reduce aggression until price clears chop" not in text
    assert "target clean" not in text.lower()
    assert "clean target" not in text.lower()


def test_short_liquidity_rejection_signal_tracks_rejection_and_invalidates_above_stop() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            direction="short",
            stop_loss=Decimal("105"),
            structure_reason="Upside liquidity was swept and rejected.",
            confluence="LTF BOS/CHoCH confirmed.",
            invalidation_reason="Invalid if price accepts above 105.",
            edge_evidence=_edge(
                sweep_direction="bearish",
                swept_level=Decimal("105"),
                sweep_wick=Decimal("105.5"),
                structure_direction="bearish",
            ),
        ),
    )

    assert "BTCUSDT · SHORT · SCALP" in text
    assert "Price swept upside liquidity at 105 with a wick to 105.5, then closed back below the level." in text
    assert "Invalid if price accepts above 105." in text
    assert "🐺 Liquidity taken. Structure confirmed. Hunt active." in text


def test_public_signal_missing_liquidity_uses_safe_wolf_fallback() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            structure_reason="Pullback zone is mapped.",
            confluence="LTF BOS/CHoCH confirmed.",
            edge_evidence=SignalEdgeEvidence(),
        ),
    )

    assert "Wolf found downside liquidity" not in text
    assert "Wolf found upside liquidity" not in text
    assert "🧠 INTELLIGENCE" not in text
    assert "🐺 Signal confirmed. Execution stays disciplined." in text


def test_public_watchlist_formatter_uses_short_bias_and_stop() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            direction="short",
            stop_loss=Decimal("105"),
            structure_reason="Upside liquidity was swept and rejected.",
            invalidation_reason="Invalid if price accepts above 105.",
        ),
    )

    assert "BTCUSDT · SHORT · SCALP" in text
    assert "SL: 105" in text
    assert "Invalid if price accepts above 105." in text
    assert "Limit Zone must hold as resistance" not in text
    assert "Bearish structure must remain valid" not in text
    assert "Invalid below/above" not in text



def test_watchlist_formatter_shape() -> None:
    test_public_watchlist_formatter_matches_compact_watch_shape()


def test_triggered_waiting_confirmation_formatter_shape() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SETUP_TRIGGERED,
        _message(
            watchlist_status="LIMIT_ZONE_HIT_WAITING_CONFIRMATION",
            structure_reason="Liquidity has been swept, but structure has not fully confirmed yet. This is a stalking setup - confirmation is still required before aggressive execution.",
        ),
    )

    assert "BTCUSDT · LONG · SCALP" in text
    assert "🟠 HUNT ACTIVE — CONFIRMATION PENDING" in text
    assert "The setup has activated, but the final confirmation gate has not been earned." in text
    assert "Wait for final confirmation." in text
    assert "Do not enter blindly or chase price." in text
    assert "🐺 Territory reached. Structure decides what happens next." in text
    assert "SIGNAL CONFIRMED" not in text
def test_watchlist_upgraded_requires_upgrade_flag() -> None:
    plain = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    upgraded = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(upgraded_from_watchlist=True),
    )

    assert "WATCHLIST UPGRADED" not in plain
    assert upgraded.startswith("🐺 BTCUSDT · LONG · SCALP")
    assert "🟢 SIGNAL CONFIRMED · WATCHLIST UPGRADED" in upgraded
    assert "🎯 TRADE MAP" in upgraded


def test_limit_zone_hit_renders_hunting_zone_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.LIMIT_HIT, _message())

    assert text.startswith("🐺 BTCUSDT · LONG")
    assert "🎯 ZONE ENGAGED" in text
    assert "Price has entered the mapped territory." in text
    assert "🟠 STATUS: REACTION REQUIRED" in text
    assert "Quality: A" in text
    assert "Entry: 100 – 102" in text
    assert "The setup is alive, but confirmation has not been earned yet." in text
    assert "Invalid if price accepts below 95." in text
    assert "Use the existing published plan only." in text
    assert "🐺 The wolf is in position. No confirmation = no chase." in text
    assert "TP1: 110" not in text
    assert "TAKE PROFIT HIT" not in text
    lowered = text.lower()
    assert "scalp signal" not in lowered
    assert "active for manual execution" not in lowered
    assert "status: confirmed" not in lowered
    assert "executing" not in lowered


def test_tp1_renders_partial_win_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP1_HIT, _message())

    assert text.startswith("✅ BTCUSDT · TP1 SECURED")
    assert "First objective reached." in text
    assert "TP1: 110" in text
    assert "The setup is progressing according to the stored plan." in text
    assert "Next:\nTP2: 115\nTP3: 120" in text
    assert "🐺 First target secured. The hunt continues." in text


def test_tp2_renders_strong_follow_through_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP2_HIT, _message())

    assert text.startswith("🔥 BTCUSDT · TP2 SECURED")
    assert "Second objective reached." in text
    assert "TP2: 115" in text
    assert "RR to TP2: 2.92R" in text
    assert "Strong follow-through from the mapped setup." in text
    assert "Remaining:\nTP3: 120" in text
    assert "🐺 Clean structure. Clean continuation." in text


def test_tp2_omits_rr_when_stored_context_does_not_provide_it() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.TP2_HIT,
        _message(planned_rr=NA, signal_context=None),
    )

    assert "🔥 BTCUSDT · TP2 SECURED" in text
    assert "RR to TP2:" not in text


def test_tp3_renders_trade_complete_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP3_HIT, _message())

    assert text.startswith("✅ BTCUSDT · FULL TARGET SEQUENCE COMPLETE")
    assert "TP3: 120" in text
    assert "The stored target sequence has completed." in text
    assert "🐺 Liquidity to expansion. Hunt complete." in text


def test_stop_hit_renders_controlled_loss_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SL_HIT, _message())

    assert text.startswith("🔴 BTCUSDT · SETUP INVALIDATED")
    assert "Stop: 95" in text
    assert "Price failed the structural thesis and the setup is closed." in text
    assert "Result: SL" in text
    assert "🧠 Outcome remains part of lifecycle and expectancy tracking." in text
    assert "No revenge. No reinterpretation. Next setup." in text


def test_watchlist_invalidated_renders_wolf_walks_away_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.INVALIDATED, _message(was_watchlist=True))

    assert text.startswith("🔴 BTCUSDT · WATCHLIST INVALIDATED")
    assert "The watchlist thesis no longer meets the required structure." in text
    assert "No entry was confirmed." in text
    assert "No weak confirmations." in text
    assert "🐺 The wolf walks away when the edge disappears." in text


def test_signal_invalidated_renders_cancelled_no_chase_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.INVALIDATED, _message(was_watchlist=False))

    assert text.startswith("🔴 BTCUSDT · SETUP INVALIDATED")
    assert "The required structure no longer supports the original thesis." in text
    assert "No chase." in text
    assert "No forced entry." in text
    assert "No weak confirmation." in text
    assert "🐺 The wolf walks away when the edge disappears." in text


def test_expired_and_no_longer_tracking_remain_distinct_public_outcomes() -> None:
    expired = format_telegram_signal_message(TelegramAlertType.EXPIRED, _message())
    no_longer_tracking = format_telegram_signal_message(
        TelegramAlertType.NO_LONGER_TRACKING,
        _message(),
    )

    assert expired.startswith("👁 BTCUSDT · WATCH EXPIRED")
    assert "expired before confirmation" in expired
    assert no_longer_tracking.startswith("👁 BTCUSDT · NO LONGER TRACKING")
    assert "no longer qualifies for active monitoring" in no_longer_tracking
    assert expired != no_longer_tracking


def test_no_trade_output_does_not_expose_internal_debug_codes() -> None:
    text = format_public_no_trade_message(
        _message(),
        "first_failed_gate=missing_confirmation_structure_shift; strategy_diagnostics={raw}",
    )

    assert "🐺🟠 NO TRADE — BTCUSDT" in text
    assert "Status: NO VALID SETUP" in text
    assert "Reason: Confirmation is not clean yet." in text
    assert "first_failed_gate" not in text
    assert "missing_confirmation_structure_shift" not in text
    assert "strategy_diagnostics" not in text


def test_missing_tp3_renders_labeled_na_target() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message(tp3=NA))

    assert "🎯 TRADE MAP" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: N/A" in text
    assert "Trade Map (incomplete stored context)" not in text
    assert "Missing: TP3" not in text


def test_missing_evidence_omits_intelligence_without_inventing_technical_claims() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            structure_reason="Upside liquidity swept into an order block.",
            confluence="M15 CHoCH confirmed with FVG and Fib alignment.",
            ob_fvg_status="OB/FVG validated.",
            edge_evidence=None,
        ),
    )

    assert "🧠 INTELLIGENCE" not in text
    for unsupported in ("liquidity swept", "CHoCH", "order block", "OB/FVG", "Fib alignment"):
        assert unsupported not in text
    assert "🐺 Signal confirmed. Execution stays disciplined." in text


def test_missing_invalidation_uses_directional_stop_fallback() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(invalidation_reason=NA),
    )

    assert "Invalid if price accepts below 95." in text


def test_swing_signal_labels_swing_setup() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message(mode="swing"))

    assert "BTCUSDT · LONG · SWING" in text
    assert "BTCUSDT · LONG · SCALP" not in text


def test_combined_source_modes_need_valid_confluence_flag() -> None:
    selected_scalp = _message(
        mode="scalp",
        signal_context=SignalMessageContext(
            symbol="BTCUSDT",
            direction="long",
            primary_mode="scalp",
            source_modes=("scalp", "swing"),
            confluence_valid=False,
        ),
    )
    confluence = _message(
        mode="scalp",
        signal_context=SignalMessageContext(
            symbol="BTCUSDT",
            direction="long",
            primary_mode="scalp",
            secondary_modes=("swing",),
            source_modes=("scalp", "swing"),
            confluence_valid=True,
        ),
    )

    assert "BTCUSDT · LONG · SCALP" in format_telegram_signal_message(TelegramAlertType.WATCHLIST, selected_scalp)
    assert "BTCUSDT · LONG · SCALP/SWING" in format_telegram_signal_message(TelegramAlertType.WATCHLIST, confluence)


def test_a_plus_grade_and_score_are_shown() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message(quality="A+", quality_score=96))

    assert "A+ · Score 96 · RR 2.92R" in text


def test_long_and_short_invalidation_are_direction_aware() -> None:
    long_text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message(invalidation_reason=NA))
    short_text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(direction="short", stop_loss=Decimal("105"), invalidation_reason=NA),
    )

    assert "Invalid if price accepts below 95." in long_text
    assert "Invalid if price accepts above 105." in short_text


def test_missing_invalidation_level_does_not_invent_number() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(stop_loss=NA, invalidation_reason=NA, watchlist_invalidation_reason=NA),
    )

    assert "Invalidation: N/A." in text
    assert "95" not in text


def test_structured_edge_evidence_replaces_generic_fallback_text() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            structure_reason="Setup quality does not provide enough deterministic edge.",
            signal_context=SignalMessageContext(
                symbol="BTCUSDT",
                direction="long",
                primary_mode="scalp",
                source_modes=("scalp",),
                why_it_matters_points=(
                    "Downside liquidity was swept before the setup mapped.",
                    "15m BOS/CHoCH confirms the structure shift.",
                    "Pullback is mapped into an OB/FVG reaction zone.",
                    "Pullback depth aligns with the fib pocket.",
                    "Target integrity leaves a clean RR path toward TP2.",
                ),
            ),
        ),
    )

    assert "Price swept downside liquidity at 99" in text
    assert "15m bullish CHoCH closed above 103 at 104." in text
    assert "selected OB/FVG overlap" in text
    assert "Setup quality does not provide enough deterministic edge" not in text
    assert "clean RR path" not in text


def _edge_section(text: str) -> str:
    if "🧠 INTELLIGENCE\n" not in text:
        return ""
    return text.split("🧠 INTELLIGENCE\n", 1)[1].split("\n\n━━━━━━━━━━━━━━", 1)[0]


def test_long_and_short_edges_use_actual_sweep_side_and_level() -> None:
    long_edge = _edge_section(
        format_telegram_signal_message(TelegramAlertType.SETUP_TRIGGERED, _message())
    )
    short_edge = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SETUP_TRIGGERED,
            _message(
                direction="short",
                edge_evidence=_edge(
                    sweep_direction="bearish",
                    swept_level=Decimal("105.25"),
                    sweep_wick=Decimal("105.40"),
                    structure_direction="bearish",
                ),
            ),
        )
    )

    assert "downside liquidity at 99" in long_edge
    assert "upside liquidity at 105.25" in short_edge
    assert long_edge != short_edge


def test_bos_and_choch_edges_preserve_exact_structure_kind() -> None:
    bos = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(structure_kind="BOS")),
        )
    )
    choch = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(structure_kind="CHoCH")),
        )
    )

    assert "bullish BOS" in bos and "CHoCH" not in bos
    assert "bullish CHoCH" in choch and " BOS" not in choch


def test_entry_edge_claims_only_the_selected_zone_evidence() -> None:
    ob = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(selected_zone_type="OB", fib_aligned=False)),
        )
    )
    fvg = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(selected_zone_type="FVG", fib_aligned=False)),
        )
    )
    overlap = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(selected_zone_type="OB_FVG_OVERLAP", fib_aligned=False)),
        )
    )

    assert "selected order block" in ob and "FVG" not in ob
    assert "selected FVG" in fvg and "order block" not in fvg
    assert "selected OB/FVG overlap" in overlap


def test_fib_and_rr_are_emitted_only_when_present() -> None:
    without = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(fib_aligned=False, pullback_depth_ratio=NA, rr_to_tp2=NA)),
        )
    )
    with_rr = _edge_section(
        format_telegram_signal_message(
            TelegramAlertType.SIGNAL_CONFIRMED,
            _message(edge_evidence=_edge(rr_to_tp2=Decimal("3.06"))),
        )
    )

    assert "fib" not in without.lower()
    assert "retracement" not in without.lower()
    assert "R to TP2" not in without
    assert "3.06R to TP2" in with_rr


def test_edge_is_deterministic_public_safe_and_ignores_research_context() -> None:
    message = _message(
        current_context=(
            "CVD confirms; orderflow supports; order-book depth is strong; "
            "liquidations reinforce the trade."
        ),
    )
    first_message = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, message)
    second_message = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, message)
    first = _edge_section(first_message)
    second = _edge_section(second_message)

    assert first_message == second_message
    assert first == second
    assert "N/A" not in first
    assert not any(
        term in first.lower()
        for term in (
            "selected_zone_type",
            "pullback_depth_ratio",
            "rr_to_tp2",
            "cvd",
            "orderflow",
            "order-book",
            "liquidation",
        )
    )


def test_all_required_stored_levels_survive_confirmed_formatting() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            entry_low=Decimal("0.21690"),
            entry_high=Decimal("0.21731"),
            stop_loss=Decimal("0.21940"),
            tp1=Decimal("0.21400"),
            tp2=Decimal("0.21089"),
            tp3=Decimal("0.20890"),
            planned_rr=Decimal("3.07"),
            edge_evidence=_edge(
                entry_low=Decimal("0.21690"),
                entry_high=Decimal("0.21731"),
                rr_to_tp2=Decimal("3.07"),
            ),
        ),
    )

    for expected in (
        "Entry: 0.2169 – 0.21731",
        "SL: 0.2194",
        "TP1: 0.214",
        "TP2: 0.21089",
        "TP3: 0.2089",
        "RR 3.07R",
    ):
        assert expected in text


def test_public_display_rename_does_not_mutate_internal_lifecycle_state() -> None:
    message = _message(lifecycle_state="TRIGGERED")

    text = format_telegram_signal_message(TelegramAlertType.SETUP_TRIGGERED, message)

    assert TelegramAlertType.SETUP_TRIGGERED.value == "SETUP_TRIGGERED"
    assert message.lifecycle_state == "TRIGGERED"
    assert "🟠 HUNT ACTIVE — CONFIRMATION PENDING" in text
    assert "TRIGGERED" not in text


def test_rejected_no_setup_output_is_not_converted_to_valid_signal() -> None:
    text = format_public_no_trade_message(_message(), "Opportunity score is below 80.")

    assert "NO TRADE" in text
    assert "SIGNAL — BTCUSDT" not in text
    assert "Status: CONFIRMED" not in text
    assert "Reason: Quality is not strong enough yet." in text


def test_price_display_formats_clean_public_values() -> None:
    assert format_telegram_price(Decimal("73.252056")) == "73.25"
    assert format_telegram_price(Decimal("109.99894")) == "110"
    assert format_telegram_price(Decimal("0.0457434")) == "0.04574"
    assert format_telegram_price(Decimal("0.16737736")) == "0.16738"
    assert format_telegram_price(Decimal("70000.123456")) == "70000.12"
    assert "E" not in format_telegram_price(Decimal("0.000000123456")).upper()
    assert format_telegram_price(NA) == NA
    assert format_telegram_price({"price": "73.25"}) == NA


def test_rr_display_formats_clean_public_values() -> None:
    assert format_telegram_rr(Decimal("2.91918017")) == "2.92R"
    assert format_telegram_rr(Decimal("3.000000")) == "3.00R"
    assert format_telegram_rr(Decimal("3.246")) == "3.25R"
    assert format_telegram_rr(NA) == NA
    assert format_telegram_rr("not numeric") == NA
