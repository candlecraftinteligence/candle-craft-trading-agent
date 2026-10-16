from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.main import create_app

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"


@pytest.fixture()
def source() -> MockFixtureSource:
    return MockFixtureSource(FIXTURE_DIR)


@pytest.fixture()
def client(source: MockFixtureSource) -> TestClient:
    return TestClient(create_app(source))
