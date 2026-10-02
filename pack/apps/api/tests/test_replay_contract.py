import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.cci.errors import FixtureContractError
from app.integrations.cci.mock_fixture_source import MockFixtureSource

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"
LEAKS = ("tp_hit", "tp1", "tp2", "sl_hit", "invalidated", "expired", "outcome", "closed", "hunt", "standard")


def _masked_text(replay: dict) -> str:
    parts = [replay["masked_title"], replay["masked_thesis"]]
    for block in replay["evidence"]:
        parts.append(block["label"])
        if block.get("detail"):
            parts.append(block["detail"])
    return "\n".join(parts).lower()


def test_closed_fixtures_have_leak_free_replay_briefs() -> None:
    files = list(FIXTURE_DIR.glob("*.json"))
    closed = 0
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        resolved = any(event["event_type"] == "SETUP_RESOLVED" for event in raw["lifecycle"])
        if not resolved:
            assert "replay" not in raw
            continue
        closed += 1
        replay = raw["replay"]
        assert replay["preferred_decision"] in {"TRACK", "TAKE", "WATCH", "NO_TRADE"}
        assert replay["teaching_note"]
        outcome = None
        for event in raw["lifecycle"]:
            if event.get("outcome_code"):
                outcome = event["outcome_code"]
        assert outcome is not None
        assert outcome in replay["teaching_note"]
        masked = _masked_text(replay)
        for leak in LEAKS:
            assert leak not in masked
        assert outcome.lower() not in masked
    assert closed >= 4


def _raw_by_id(cci_setup_id: str) -> dict:
    for path in FIXTURE_DIR.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw["cci_setup_id"] == cci_setup_id:
            return raw
    raise AssertionError(cci_setup_id)


def test_api_returns_replay_without_rewriting_lifecycle(client: TestClient) -> None:
    listed = client.get("/api/missions")
    assert listed.status_code == 200
    closed = [row for row in listed.json()["missions"] if row["resolved"]]
    assert len(closed) >= 4
    for row in closed:
        detail = client.get(f"/api/missions/{row['cci_setup_id']}")
        assert detail.status_code == 200
        body = detail.json()
        file_raw = _raw_by_id(row["cci_setup_id"])
        assert body["lifecycle"] == file_raw["lifecycle"]
        assert body["replay"] == file_raw["replay"]
        assert body["replay"]["masked_thesis"] != body["thesis_summary"]


def test_resolved_fixture_without_replay_is_rejected(tmp_path: Path) -> None:
    payload = {
        "cci_setup_id": "mock_setup_missing_replay",
        "symbol": "BTCUSDT",
        "timeframe": "H1",
        "direction": "LONG",
        "quality_tier": "STANDARD",
        "title": "Missing brief",
        "thesis_summary": "Resolved without a training brief.",
        "disclaimer": "Synthetic.",
        "synthetic": True,
        "evidence": [{"type": "structure", "label": "Base"}],
        "lifecycle": [
            {
                "cci_event_id": "evt_missing_01",
                "event_type": "SETUP_PUBLISHED",
                "state": "ACTIVE",
                "occurred_at": "2026-09-01T00:00:00Z",
            },
            {
                "cci_event_id": "evt_missing_02",
                "event_type": "SETUP_RESOLVED",
                "state": "CLOSED",
                "occurred_at": "2026-09-01T01:00:00Z",
                "outcome_code": "TP_HIT",
            },
        ],
    }
    (tmp_path / "missing.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureContractError, match="replay brief"):
        MockFixtureSource(tmp_path)
