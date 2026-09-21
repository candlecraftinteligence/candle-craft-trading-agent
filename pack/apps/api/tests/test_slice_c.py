"""Slice C adversarial checks: conceal, caps, rate limits, webhook, spoofed sessions."""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.api.deps import limit
from app.db.models import ReplayAttempt, User, XpLedger
from app.db.session import get_engine, reset_engine, session_factory
from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.main import create_app
from app.services.actions import REPLAY_DISCLAIMER
from tests.test_phase1_api import ENV, sign_init_data

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"
AVAX = "mock_setup_avax_h1_tp"
CONCEALED_KEYS = {
    "cci_setup_id",
    "timeframe",
    "direction",
    "masked_title",
    "masked_thesis",
    "evidence",
    "attempted",
    "disclaimer",
    "concealed",
}


@pytest.fixture(scope="module")
def migrated() -> None:
    previous = {key: os.environ.get(key) for key in ENV}
    os.environ.update(ENV)
    os.environ.pop("PACK_WEBHOOK_SECRET", None)
    os.environ.pop("PACK_TELEGRAM_BOT_TOKEN", None)
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


def _auth(api: TestClient, telegram_id: int) -> str:
    init_data = sign_init_data({"id": telegram_id, "first_name": "Ada"}, int(time.time()))
    response = api.post("/api/auth/telegram", json={"init_data": init_data, "telegram_user_id": 999})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _perfect(key: str) -> dict:
    return {
        "chosen_tier": "HUNT",
        "chosen_decision": "TRACK",
        "evidence_reviewed": True,
        "idempotency_key": key,
        "score": 0,
        "xp": 9999,
    }


def test_limit_rejects_the_next_hit_inside_the_window() -> None:
    from app.api.deps import _hits

    _hits.clear()
    limit("slice-c-unit", 2, 60)
    limit("slice-c-unit", 2, 60)
    with pytest.raises(HTTPException) as caught:
        limit("slice-c-unit", 2, 60)
    assert caught.value.status_code == 429


def test_replay_brief_conceals_until_reveal_and_cannot_be_farmed(api: TestClient) -> None:
    token = _auth(api, 101)
    headers = _headers(token)
    assert api.get(f"/api/missions/{AVAX}/replay").status_code == 401

    brief = api.get(f"/api/missions/{AVAX}/replay", headers=headers)
    assert brief.status_code == 200, brief.text
    hidden = brief.json()
    assert set(hidden) <= CONCEALED_KEYS
    assert hidden["concealed"] is True
    assert hidden["attempted"] is False
    assert hidden["disclaimer"] == REPLAY_DISCLAIMER
    blob = json.dumps(hidden)
    assert "AVAXUSDT" not in blob
    assert "TP_HIT" not in blob
    assert "teaching" not in blob

    listing = api.get("/api/replay", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["disclaimer"] == REPLAY_DISCLAIMER
    tapes = listing.json()["tapes"]
    assert tapes
    assert all(set(tape) <= CONCEALED_KEYS and tape["concealed"] is True for tape in tapes)
    assert "TP_HIT" not in json.dumps(tapes)
    assert "AVAXUSDT" not in json.dumps(tapes)

    open_tape = api.get("/api/missions/mock_setup_btc_h1_active/replay", headers=headers)
    assert open_tape.status_code == 404
    assert api.get("/api/missions/not-a-mission/replay", headers=headers).status_code == 404

    first = api.post(f"/api/missions/{AVAX}/replay", headers=headers, json=_perfect("farm-1"))
    assert first.status_code == 200, first.text
    revealed = first.json()
    assert revealed["concealed"] is False
    assert revealed["created"] is True
    assert revealed["symbol"] == "AVAXUSDT"
    assert revealed["outcome_code"] == "TP_HIT"
    assert "TRACK" in revealed["teaching_note"]
    assert revealed["score"] == 100
    assert revealed["xp_awarded"] == 40
    assert revealed["disclaimer"] == REPLAY_DISCLAIMER

    again = api.post(f"/api/missions/{AVAX}/replay", headers=headers, json=_perfect("farm-1"))
    assert again.json()["created"] is False
    assert again.json()["xp_awarded"] == 0
    assert again.json()["score"] == 100
    assert again.json()["symbol"] == "AVAXUSDT"

    after = api.get(f"/api/missions/{AVAX}/replay", headers=headers).json()
    assert after["attempted"] is True
    assert after["concealed"] is True
    assert "symbol" not in after

    db = session_factory()()
    try:
        user = db.scalar(select(User).where(User.telegram_user_id == 101))
        assert user is not None
        attempts = db.scalar(select(func.count(ReplayAttempt.id)).where(ReplayAttempt.user_id == user.id))
        ledger = db.scalar(
            select(func.count(XpLedger.id)).where(XpLedger.user_id == user.id, XpLedger.category == "replay")
        )
        assert attempts == 1
        assert ledger == 1
    finally:
        db.close()


def test_replay_daily_cap_clamps_a_new_attempt(api: TestClient) -> None:
    token = _auth(api, 102)
    db = session_factory()()
    try:
        user = db.scalar(select(User).where(User.telegram_user_id == 102))
        assert user is not None
        db.add(
            XpLedger(
                user_id=user.id,
                amount=90,
                category="replay",
                reason_code="replay",
                idempotency_key=f"seed-replay-cap-{user.id}",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    response = api.post(f"/api/missions/{AVAX}/replay", headers=_headers(token), json=_perfect("capped"))
    assert response.status_code == 200, response.text
    assert response.json()["created"] is True
    assert response.json()["score"] == 100
    assert response.json()["xp_awarded"] == 0
    assert response.json()["outcome_code"] == "TP_HIT"
    me = api.get("/api/me", headers=_headers(token)).json()
    assert me["pack_xp"] == 90


def test_auth_rate_limit_stops_init_data_guessing(api: TestClient) -> None:
    statuses = [api.post("/api/auth/telegram", json={"init_data": ""}).status_code for _ in range(30)]
    assert statuses == [401] * 30
    blocked = api.post("/api/auth/telegram", json={"init_data": sign_init_data({"id": 1, "first_name": "Ada"}, int(time.time()))})
    assert blocked.status_code == 429


def test_forged_session_is_rejected(api: TestClient) -> None:
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "tg": 1, "exp": int(time.time()) + 3600},
        "not-the-session-secret",
        algorithm="HS256",
    )
    assert api.get("/api/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    none_token = jwt.encode({"sub": str(uuid.uuid4()), "tg": 1}, "", algorithm="none")
    assert api.get("/api/me", headers={"Authorization": f"Bearer {none_token}"}).status_code == 401


def test_webhook_requires_a_configured_secret(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PACK_WEBHOOK_SECRET", raising=False)
    missing = api.post("/api/telegram/webhook", json={"update_id": 1})
    assert missing.status_code == 503

    monkeypatch.setenv("PACK_WEBHOOK_SECRET", "pack-webhook-test")
    rejected = api.post(
        "/api/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
    )
    assert rejected.status_code == 401
    accepted = api.post(
        "/api/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "pack-webhook-test"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True, "handled": False}


def test_thin_replay_bodies_are_rejected(api: TestClient) -> None:
    token = _auth(api, 103)
    bad = api.post(
        f"/api/missions/{AVAX}/replay",
        headers=_headers(token),
        json={"chosen_tier": "LEGENDARY", "chosen_decision": "YOLO", "idempotency_key": "x"},
    )
    assert bad.status_code == 422
    blank = api.post(
        f"/api/missions/{AVAX}/replay",
        headers=_headers(token),
        json={"chosen_tier": "HUNT", "chosen_decision": "TRACK", "idempotency_key": "   "},
    )
    assert blank.status_code == 422


def test_reduced_motion_and_sticky_decision_panel_stay_in_css() -> None:
    css = (Path(__file__).resolve().parents[3] / "apps" / "web" / "src" / "styles" / "global.css").read_text()
    assert "prefers-reduced-motion: reduce" in css
    assert 'html[data-motion="off"]' in css
    panel = css.split(".decision-panel", 1)[1].split("}", 1)[0]
    assert "position: sticky" in panel
    shell = css.split(".shell {", 1)[1].split("}", 1)[0]
    assert "overflow: clip" not in shell


def test_live_cci_still_refuses_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_CCI", "true")
    monkeypatch.setenv("CCI_SOURCE", "mock")
    with pytest.raises(RuntimeError, match="LIVE_CCI"):
        create_app()
