from datetime import datetime, timedelta, timezone

from app.domain.progression import (
    DECISION_XP,
    apply_daily_cap,
    next_streak,
    rank_name,
    replay_base_xp,
    streak_bonus_for,
)


def test_no_trade_is_worth_at_least_as_much_as_i_took_this() -> None:
    assert DECISION_XP["NO_TRADE"] >= DECISION_XP["I_TOOK_THIS"]
    assert DECISION_XP["NO_TRADE"] == 25
    assert DECISION_XP["I_TOOK_THIS"] == 20


def test_daily_cap_halts_and_diminishes_after_seventy_percent() -> None:
    assert apply_daily_cap("decision", 0, 25) == 25
    assert apply_daily_cap("decision", 50, 25) == 25
    assert apply_daily_cap("decision", 75, 25) == 5
    assert apply_daily_cap("decision", 80, 25) == 0
    assert apply_daily_cap("streak", 0, 10) == 10


def test_replay_diminishing_returns() -> None:
    assert replay_base_xp(0, 0) == 15
    assert replay_base_xp(2, 0) == 15
    assert replay_base_xp(3, 0) == 7
    assert replay_base_xp(4, 0) == 7
    assert replay_base_xp(5, 0) == 3
    assert replay_base_xp(0, 80) == 40
    assert replay_base_xp(5, 100) == int(40 * 0.25)


def test_rank_thresholds_and_streak_grace() -> None:
    assert rank_name(0) == "SCOUT"
    assert rank_name(250) == "TRACKER"
    assert rank_name(8499) == "VANGUARD"
    assert rank_name(8500) == "ELITE"
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    assert next_streak(0, None, now) == 1
    assert next_streak(2, now - timedelta(hours=2), now) == 2
    assert next_streak(2, now - timedelta(days=1), now) == 3
    missed = datetime(2026, 9, 19, 23, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    assert later - missed <= timedelta(hours=36)
    assert next_streak(4, missed, later) == 4
    assert next_streak(4, now - timedelta(hours=48), now) == 1
    assert streak_bonus_for(3, 2) == 10
    assert streak_bonus_for(3, 3) is None
    assert streak_bonus_for(21, 20) == 10
