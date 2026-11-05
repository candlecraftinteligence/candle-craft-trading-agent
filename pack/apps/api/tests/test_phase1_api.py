import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Quest, QuestCompletion, User, UserAchievement, XpLedger
from app.db.session import get_engine, reset_engine, session_factory
from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.main import create_app
from app.security.telegram_auth import AuthError, validate_init_data

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"
TEST_URL = "postgresql+psycopg://pack:pack@127.0.0.1:5432/pack_test"
BOT = "123456:TESTTOKEN"

ENV = {
    "DATABASE_URL": TEST_URL,
    "PACK_BOT_TOKEN": BOT,
    "PACK_SESSION_SECRET": "test-secret-not-for-production",
    "DEV_BROWSER_MODE": "true",
    "PACK_ENV": "dev",
    "APP_ENV": "dev",
    "LIVE_CCI": "false",
    "CCI_SOURCE": "mock",
    "AUTH_MAX_AGE_SECONDS": "86400",
}


def sign_init_data(user: dict, auth_date: int, bot_token: str = BOT) -> str:
    pairs = {
        "auth_date": str(auth_date),
        "query_id": "AAE",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(pairs)


@pytest.fixture(scope="module")
def migrated() -> None:
    previous = {key: os.environ.get(key) for key in ENV}
    os.environ.update(ENV)
    reset_engine()
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_ROOT, check=True, env=os.environ.copy())
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_engine()


@pytest.fixture()
def source() -> MockFixtureSource:
    return MockFixtureSource(FIXTURE_DIR)


@pytest.fixture()
def api(migrated: None, source: MockFixtureSource) -> TestClient:
    reset_engine()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE replay_attempts, replay_challenges, user_achievements, achievements, "
                "quest_completions, quests, xp_ledger, journals, user_mission_decisions, "
                "mission_lifecycle_events, missions, users CASCADE"
            )
        )
    from app.api.deps import _hits

    _hits.clear()
    with TestClient(create_app(source)) as client:
        yield client


def _auth(api: TestClient, telegram_id: int, name: str = "Ada") -> str:
    init_data = sign_init_data({"id": telegram_id, "first_name": name, "username": name.lower()}, int(time.time()))
    response = api.post("/api/auth/telegram", json={"init_data": init_data, "telegram_user_id": 1})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_migrations_are_idempotent_and_uniqueness_is_enforced(migrated: None) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_ROOT, check=True, env=os.environ.copy())
    from app.db.models import Achievement

    reset_engine()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE replay_attempts, replay_challenges, user_achievements, achievements, "
                "quest_completions, quests, xp_ledger, journals, user_mission_decisions, "
                "mission_lifecycle_events, missions, users CASCADE"
            )
        )
    db = session_factory()()
    try:
        user = User(telegram_user_id=4242, display_name="Ada")
        db.add(user)
        db.flush()
        db.add(User(telegram_user_id=4242, display_name="Duplicate"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

        user = User(telegram_user_id=4243, display_name="Bea")
        achievement = Achievement(code="T01", name="Test", category="Process", rarity="Common", rule_json={}, xp_reward=50)
        quest = Quest(code="sample-unique", cadence="daily", title="Sample", rule_json={}, xp_reward=40)
        db.add_all([user, achievement, quest])
        db.flush()
        db.add(QuestCompletion(user_id=user.id, quest_id=quest.id, period_key="2026-09-21"))
        db.flush()
        db.add(QuestCompletion(user_id=user.id, quest_id=quest.id, period_key="2026-09-21"))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

        user = User(telegram_user_id=4244, display_name="Cy")
        achievement = Achievement(code="T02", name="Test", category="Process", rarity="Common", rule_json={}, xp_reward=50)
        db.add_all([user, achievement])
        db.flush()
        db.add(UserAchievement(user_id=user.id, achievement_id=achievement.id))
        db.flush()
        db.add(UserAchievement(user_id=user.id, achievement_id=achievement.id))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    finally:
        db.close()


def test_init_data_accepts_valid_and_rejects_abuse(api: TestClient) -> None:
    now = int(time.time())
    good = sign_init_data({"id": 7, "first_name": "Ada"}, now)
    ok = api.post("/api/auth/telegram", json={"init_data": good})
    assert ok.status_code == 200
    assert ok.json()["user"]["telegram_user_id"] == 7

    missing = api.post("/api/auth/telegram", json={"init_data": ""})
    assert missing.status_code == 401

    parsed = dict(urllib.parse.parse_qsl(good))
    parsed["hash"] = "0" * 64
    bad_hash = api.post("/api/auth/telegram", json={"init_data": urllib.parse.urlencode(parsed)})
    assert bad_hash.status_code == 401

    expired = sign_init_data({"id": 8, "first_name": "Ada"}, now - 90_000)
    assert api.post("/api/auth/telegram", json={"init_data": expired}).status_code == 401

    tampered = dict(urllib.parse.parse_qsl(good))
    tampered["user"] = json.dumps({"id": 999, "first_name": "Spoof"}, separators=(",", ":"))
    assert api.post("/api/auth/telegram", json={"init_data": urllib.parse.urlencode(tampered)}).status_code == 401

    with pytest.raises(AuthError):
        validate_init_data("", BOT, max_age_seconds=86400, now=now)


def test_dev_browser_is_rejected_in_production(migrated: None, source: MockFixtureSource, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACK_ENV", "production")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEV_BROWSER_MODE", "true")
    monkeypatch.setenv("LIVE_CCI", "false")
    with TestClient(create_app(source)) as client:
        assert client.post("/api/auth/dev").status_code == 404


def test_live_cci_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_CCI", "true")
    with pytest.raises(RuntimeError, match="LIVE_CCI"):
        create_app()


def test_decision_is_immutable_idempotent_and_does_not_trust_body_identity(api: TestClient) -> None:
    token = _auth(api, 11)
    other = _auth(api, 12, "Bea")
    mission = "mock_setup_btc_h1_active"
    first = api.post(
        f"/api/missions/{mission}/decision",
        headers=_headers(token),
        json={"decision": "NO_TRADE", "telegram_user_id": 12, "xp": 9999},
    )
    assert first.status_code == 200, first.text
    assert first.json()["decision"] == "NO_TRADE"
    assert first.json()["xp_awarded"] == 25

    retry = api.post(
        f"/api/missions/{mission}/decision",
        headers=_headers(token),
        json={"decision": "NO_TRADE", "xp": 9999},
    )
    assert retry.status_code == 200
    assert retry.json()["created"] is False
    assert retry.json()["xp_awarded"] == 0

    change = api.post(
        f"/api/missions/{mission}/decision",
        headers=_headers(token),
        json={"decision": "TRACK"},
    )
    assert change.status_code == 409
    assert change.json()["decision"] == "NO_TRADE"

    me = api.get("/api/me", headers=_headers(token))
    assert me.status_code == 200
    body = me.json()
    assert body["decisions"][mission] == "NO_TRADE"
    assert body["pack_xp"] == 25
    assert body["wolf_rank"] == "SCOUT"
    assert "outcome_code" not in body

    stranger = api.get(f"/api/missions/{mission}/decision", headers=_headers(other))
    assert stranger.json()["decision"] is None
    unauth = api.post(f"/api/missions/{mission}/decision", json={"decision": "TRACK", "telegram_user_id": 11})
    assert unauth.status_code == 401

    db = session_factory()()
    try:
        rows = db.scalars(select(XpLedger).where(XpLedger.category == "decision")).all()
        assert len(rows) == 1
        assert rows[0].amount == 25
    finally:
        db.close()


def test_decision_cap_and_no_trade_versus_entry(api: TestClient) -> None:
    token = _auth(api, 21)
    missions = api.get("/api/missions").json()["missions"]
    awarded = []
    for index, mission in enumerate(missions[:5]):
        response = api.post(
            f"/api/missions/{mission['cci_setup_id']}/decision",
            headers=_headers(token),
            json={"decision": "NO_TRADE"},
        )
        assert response.status_code == 200, response.text
        awarded.append(response.json()["xp_awarded"])
    assert awarded[0] == 25
    assert sum(awarded) == 80
    assert awarded[-1] == 0
    me = api.get("/api/me", headers=_headers(token)).json()
    assert me["pack_xp"] == 80

    other = _auth(api, 22, "Bea")
    took = api.post(
        "/api/missions/mock_setup_btc_h1_active/decision",
        headers=_headers(other),
        json={"decision": "I_TOOK_THIS"},
    )
    passed = api.post(
        "/api/missions/mock_setup_eth_h4_watch/decision",
        headers=_headers(other),
        json={"decision": "NO_TRADE"},
    )
    assert took.json()["xp_awarded"] == 20
    assert passed.json()["xp_awarded"] == 25
    assert passed.json()["xp_awarded"] >= took.json()["xp_awarded"]


def test_journal_cannot_farm_or_rewrite_cci_outcome(api: TestClient) -> None:
    token = _auth(api, 31)
    other = _auth(api, 32, "Bea")
    mission_id = "mock_setup_avax_h1_tp"
    mission = api.get(f"/api/missions/{mission_id}").json()
    assert mission["outcome_code"] == "TP_HIT"
    assert "self_reported_result" not in mission
    assert "process_notes" not in mission

    payload = {
        "process_notes": "Waited for the base.",
        "emotional_state": "MEASURED",
        "followed_plan": "Base low is the invalidation.",
        "self_reported_result": "WIN",
        "lesson": "A pass would have been complete.",
        "pnl": 5000,
        "leverage": 20,
        "size": 3,
        "outcome_code": "SL_HIT",
        "xp": 9999,
    }
    first = api.post(f"/api/missions/{mission_id}/journal", headers=_headers(token), json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["xp_awarded"] == 30
    assert first.json()["journal"]["self_reported_result"] == "WIN"
    assert "outcome_code" not in first.json()["journal"]

    loss = dict(payload, self_reported_result="LOSS", pnl=-8000, leverage=50, size=9)
    second_mission = "mock_setup_link_m15_sl"
    loss_response = api.post(f"/api/missions/{second_mission}/journal", headers=_headers(token), json=loss)
    assert loss_response.status_code == 200, loss_response.text
    assert loss_response.json()["xp_awarded"] == 30

    again = api.post(f"/api/missions/{mission_id}/journal", headers=_headers(token), json=payload)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["xp_awarded"] == 0

    me = api.get("/api/me", headers=_headers(token)).json()
    assert me["pack_xp"] == 60
    fresh = api.get(f"/api/missions/{mission_id}").json()
    assert fresh["outcome_code"] == "TP_HIT"
    hidden = api.get(f"/api/missions/{mission_id}/journal", headers=_headers(other)).json()
    assert hidden["journal"] is None


def test_replay_score_is_server_side_and_diminishes(api: TestClient) -> None:
    token = _auth(api, 41)
    mission_id = "mock_setup_avax_h1_tp"
    awarded = []
    for index in range(6):
        response = api.post(
            f"/api/missions/{mission_id}/replay",
            headers=_headers(token),
            json={
                "chosen_tier": "STANDARD",
                "chosen_decision": "TAKE",
                "evidence_reviewed": False,
                "idempotency_key": f"try-{index}",
                "score": 100,
                "xp": 9999,
            },
        )
        assert response.status_code == 200, response.text
        awarded.append(response.json()["xp_awarded"])
        assert response.json()["score"] == 0
    assert awarded == [15, 15, 15, 7, 7, 3]
    duplicate = api.post(
        f"/api/missions/{mission_id}/replay",
        headers=_headers(token),
        json={
            "chosen_tier": "HUNT",
            "chosen_decision": "TRACK",
            "evidence_reviewed": True,
            "idempotency_key": "try-0",
            "score": 100,
        },
    )
    assert duplicate.json()["created"] is False
    assert duplicate.json()["xp_awarded"] == 0
    total = api.get("/api/me", headers=_headers(token)).json()["pack_xp"]
    assert total == sum(awarded)


def test_streak_advances_on_a_process_action(api: TestClient) -> None:
    token = _auth(api, 51)
    api.post(
        "/api/missions/mock_setup_btc_h1_active/decision",
        headers=_headers(token),
        json={"decision": "WATCH_ONLY"},
    )
    db = session_factory()()
    try:
        user = db.scalar(select(User).where(User.telegram_user_id == 51))
        assert user is not None
        assert user.discipline_streak == 1
        user.discipline_streak = 2
        user.last_process_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    finally:
        db.close()
    response = api.post(
        "/api/missions/mock_setup_eth_h4_watch/decision",
        headers=_headers(token),
        json={"decision": "NO_TRADE"},
    )
    assert response.status_code == 200
    me = api.get("/api/me", headers=_headers(token)).json()
    assert me["discipline_streak"] == 3
    db = session_factory()()
    try:
        bonus = db.scalar(
            select(func.coalesce(func.sum(XpLedger.amount), 0)).where(
                XpLedger.category == "streak",
                XpLedger.reason_code == "streak:3",
            )
        )
        assert int(bonus or 0) == 10
    finally:
        db.close()
