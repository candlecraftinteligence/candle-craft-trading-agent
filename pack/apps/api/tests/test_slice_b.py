import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.bot.messages import NOTICES, copy_is_calm, outbound_notice, start_reply, telegram_startapp_url
from app.bot.runtime import ensure_bot_optional, main
from app.db.models import QuestCompletion, User, UserAchievement, XpLedger
from app.db.session import get_engine, reset_engine, session_factory
from app.domain.board import (
    DAILY_QUESTS,
    WEEKLY_QUESTS,
    normalize_prefs,
    pick_codes,
    quiet_week,
    templates_are_process_only,
)
from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.main import create_app
from tests.test_phase1_api import ENV, sign_init_data

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"
BANNED = re.compile(r"take\s+\d+\s+trades|most\s+trades|win[-\s]?rate|\bleverage\b|\bpnl\b", re.I)


@pytest.fixture(scope="module")
def migrated() -> None:
    previous = {key: os.environ.get(key) for key in ENV}
    os.environ.update(ENV)
    os.environ.pop("PACK_TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("BOT_REQUIRED", None)
    reset_engine()
    import subprocess

    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_ROOT, check=True, env=os.environ.copy())
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_engine()


@pytest.fixture()
def api(migrated: None) -> TestClient:
    reset_engine()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE replay_attempts, replay_challenges, process_marks, user_achievements, achievements, "
                "quest_completions, quests, xp_ledger, journals, user_mission_decisions, "
                "mission_lifecycle_events, missions, users CASCADE"
            )
        )
    from app.api.deps import _hits

    _hits.clear()
    source = MockFixtureSource(FIXTURE_DIR)
    with TestClient(create_app(source)) as client:
        yield client


def _auth(api: TestClient, telegram_id: int, name: str = "Ada") -> str:
    init_data = sign_init_data({"id": telegram_id, "first_name": name}, int(time.time()))
    response = api.post("/api/auth/telegram", json={"init_data": init_data, "telegram_user_id": 1})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_quest_templates_are_deterministic_and_process_only() -> None:
    assert templates_are_process_only() == []
    first = pick_codes(DAILY_QUESTS, 3, "user-1:2026-09-21")
    assert first == pick_codes(DAILY_QUESTS, 3, "user-1:2026-09-21")
    assert len(first) == 3
    assert len(pick_codes(WEEKLY_QUESTS, 2, "user-1:2026-W39")) == 2
    for quest in (*DAILY_QUESTS, *WEEKLY_QUESTS):
        assert BANNED.search(f"{quest.title} {quest.detail}") is None


def test_quiet_week_rejects_a_take_and_a_gap() -> None:
    today = date(2026, 9, 21)
    days = {today - timedelta(days=offset): {"process"} for offset in range(7)}
    assert quiet_week(days, today) is True
    days[today] = {"process", "I_TOOK_THIS"}
    assert quiet_week(days, today) is False


def test_notification_defaults_and_bot_copy() -> None:
    prefs = normalize_prefs({})
    assert prefs["new_mission"] is True
    assert prefs["lifecycle_resolution"] is True
    assert prefs["quest_complete"] is False
    assert prefs["streak"] is False
    assert prefs["replay_nudge"] is False
    assert outbound_notice(prefs, "new_mission", "mock_setup_btc_h1_active")["text"] == NOTICES["new_mission"]
    assert outbound_notice(prefs, "quest_complete") is None
    assert outbound_notice({"quest_complete": True}, "quest_complete")["kind"] == "quest_complete"
    reply = start_reply("https://pack.example", "mock_setup_btc_h1_active")
    assert reply["web_app_url"] == "https://pack.example/missions/mock_setup_btc_h1_active"
    assert "Open The Pack" == reply["button_text"]
    assert start_reply("https://pack.example", "../etc")["mission_id"] is None
    assert telegram_startapp_url("PackBot", "mock_setup_btc_h1_active").endswith("startapp=mock_setup_btc_h1_active")
    assert copy_is_calm() == []


def test_bot_stays_disabled_without_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PACK_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_REQUIRED", raising=False)
    assert main() == 0
    assert ensure_bot_optional() == "disabled"
    monkeypatch.setenv("BOT_REQUIRED", "true")
    monkeypatch.setenv("PACK_TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit):
        main()
    with pytest.raises(RuntimeError, match="PACK_TELEGRAM_BOT_TOKEN"):
        create_app()


def test_unknown_mission_deep_link_is_not_found(api: TestClient) -> None:
    missing = api.get("/api/missions/not_a_real_mission")
    assert missing.status_code == 404


def test_quests_and_achievements_award_once(api: TestClient) -> None:
    token = _auth(api, 71)
    headers = _headers(token)
    locked = api.post(
        "/api/missions/mock_setup_btc_h1_active/decision",
        headers=headers,
        json={"decision": "NO_TRADE", "xp": 9999},
    )
    assert locked.json()["xp_awarded"] == 25
    journal = api.post(
        "/api/missions/mock_setup_avax_h1_tp/journal",
        headers=headers,
        json={
            "process_notes": "Waited.",
            "emotional_state": "MEASURED",
            "followed_plan": "Base low.",
            "self_reported_result": "LOSS",
            "lesson": "A pass was available.",
            "pnl": 4000,
            "leverage": 20,
            "size": 2,
        },
    )
    assert journal.json()["xp_awarded"] == 30
    replay = api.post(
        "/api/missions/mock_setup_avax_h1_tp/replay",
        headers=headers,
        json={
            "chosen_tier": "HUNT",
            "chosen_decision": "TRACK",
            "evidence_reviewed": True,
            "idempotency_key": "once",
            "score": 100,
            "xp": 9999,
        },
    )
    assert replay.status_code == 200, replay.text
    mark = api.post(
        "/api/missions/mock_setup_btc_h1_active/mark",
        headers=headers,
        json={"kind": "evidence", "xp": 9999},
    )
    assert mark.json()["created"] is True
    assert mark.json()["xp_awarded"] == 5
    again_mark = api.post(
        "/api/missions/mock_setup_btc_h1_active/mark",
        headers=headers,
        json={"kind": "evidence", "xp": 9999},
    )
    assert again_mark.json()["created"] is False
    assert again_mark.json()["xp_awarded"] == 0

    before = api.get("/api/me", headers=headers).json()["pack_xp"]
    assert before == 25 + 30 + replay.json()["xp_awarded"] + 5

    first = api.get("/api/quests", headers=headers)
    assert first.status_code == 200, first.text
    board = first.json()
    assert len(board["daily"]) == 3
    assert len(board["weekly"]) == 2
    for quest in board["daily"] + board["weekly"]:
        assert BANNED.search(f"{quest['title']} {quest['detail']}") is None
    assert all(quest["completed"] for quest in board["daily"])
    assert not any(quest["completed"] for quest in board["weekly"])

    settled = api.get("/api/me", headers=headers).json()["pack_xp"]
    assert settled > before
    second = api.get("/api/quests", headers=headers).json()
    assert [quest["completed"] for quest in second["daily"]] == [True, True, True]
    assert api.get("/api/me", headers=headers).json()["pack_xp"] == settled

    unlocked = api.get("/api/achievements", headers=headers).json()["unlocked"]
    assert "A01" in unlocked
    assert "A05" in unlocked
    assert api.get("/api/achievements", headers=headers).json()["unlocked"] == unlocked
    assert api.get("/api/me", headers=headers).json()["pack_xp"] == settled

    db = session_factory()()
    try:
        user = db.scalar(select(User).where(User.telegram_user_id == 71))
        completions = db.scalar(select(func.count(QuestCompletion.id)).where(QuestCompletion.user_id == user.id))
        quest_rows = db.scalar(
            select(func.count(XpLedger.id)).where(XpLedger.user_id == user.id, XpLedger.category == "quest")
        )
        achievement_rows = db.scalar(
            select(func.count(XpLedger.id)).where(XpLedger.user_id == user.id, XpLedger.category == "achievement")
        )
        achievement_links = db.scalar(
            select(func.count(UserAchievement.id)).where(UserAchievement.user_id == user.id)
        )
        assert completions == 3
        assert quest_rows == 3
        assert achievement_rows == achievement_links
        assert achievement_links == len(unlocked)
    finally:
        db.close()


def test_prefs_defaults_belong_to_the_session_user(api: TestClient) -> None:
    token = _auth(api, 81)
    other = _auth(api, 82, "Bea")
    me = api.get("/api/me", headers=_headers(token)).json()
    assert me["notification_prefs"]["new_mission"] is True
    assert me["notification_prefs"]["quest_complete"] is False
    assert me["notification_prefs"]["replay_nudge"] is False
    before = me["pack_xp"]
    updated = api.post(
        "/api/me/notification-prefs",
        headers=_headers(token),
        json={"quest_complete": True, "streak": True, "xp": 9999, "telegram_user_id": 82},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["notification_prefs"]["quest_complete"] is True
    assert updated.json()["notification_prefs"]["new_mission"] is True
    assert api.get("/api/me", headers=_headers(token)).json()["pack_xp"] == before
    stranger = api.get("/api/me", headers=_headers(other)).json()
    assert stranger["notification_prefs"]["quest_complete"] is False
    assert stranger["notification_prefs"]["streak"] is False
