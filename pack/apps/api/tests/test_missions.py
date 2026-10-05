import json
from pathlib import Path

from fastapi.testclient import TestClient

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"


def _fixtures() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))]


def test_list_missions_returns_every_fixture(client: TestClient) -> None:
    raw_files = _fixtures()
    response = client.get("/api/missions")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "mock"
    missions = body["missions"]
    assert len(missions) == len(raw_files)
    assert {row["cci_setup_id"] for row in missions} == {row["cci_setup_id"] for row in raw_files}
    assert all("next_state" not in row and "predicted_state" not in row for row in missions)


def test_mission_detail_returns_lifecycle_events_as_stored(client: TestClient) -> None:
    for raw in _fixtures():
        response = client.get(f"/api/missions/{raw['cci_setup_id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["lifecycle"] == raw["lifecycle"]
        assert body["evidence"] == raw["evidence"]
        assert body["thesis_summary"] == raw["thesis_summary"]
        assert body["quality_tier"] == raw["quality_tier"]
        assert body["lifecycle_state"] == raw["lifecycle"][-1]["state"]
        assert body["synthetic"] is True
        resolved = any(event["event_type"] == "SETUP_RESOLVED" for event in raw["lifecycle"])
        assert body["resolved"] is resolved
        expected_outcome = None
        for event in raw["lifecycle"]:
            if event.get("outcome_code"):
                expected_outcome = event["outcome_code"]
        assert body["outcome_code"] == expected_outcome
        if resolved:
            assert body["resolved_at"] == raw["lifecycle"][-1]["occurred_at"]
        else:
            assert body["resolved_at"] is None


def test_unknown_mission_is_not_found(client: TestClient) -> None:
    response = client.get("/api/missions/not-a-fixture")
    assert response.status_code == 404
    assert response.json()["detail"] == "Mission not found"
