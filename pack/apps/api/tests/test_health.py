from fastapi.testclient import TestClient


def test_health_reports_mock_source(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "cci-the-pack",
        "source": "mock",
    }
