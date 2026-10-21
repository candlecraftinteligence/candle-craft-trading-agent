import json
from pathlib import Path

import pytest

from app.integrations.cci.errors import FixtureContractError, SetupNotFoundError
from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.integrations.cci.protocol import CciMissionSource
from app.main import create_app

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "missions"

REQUIRED_STATES = {
    "ACTIVE",
    "WATCH",
    "STALKING",
    "TRIGGERED",
    "CONFIRMED",
    "TP1",
    "TP2",
    "TP_HIT",
    "SL_HIT",
    "INVALIDATED",
    "EXPIRED",
    "CLOSED",
}


def test_fixture_files_cover_lifecycle_paths() -> None:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert len(files) >= 8

    states: set[str] = set()
    tiers: set[str] = set()
    directions: set[str] = set()
    symbols: set[str] = set()
    timeframes: set[str] = set()

    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["synthetic"] is True
        assert "hunt" not in raw
        assert "hunts" not in raw
        tiers.add(raw["quality_tier"])
        directions.add(raw["direction"])
        symbols.add(raw["symbol"])
        timeframes.add(raw["timeframe"])
        assert raw["lifecycle"], path.name
        for event in raw["lifecycle"]:
            assert event["cci_event_id"]
            states.add(event["state"])

    assert tiers == {"STANDARD", "HUNT"}
    assert directions == {"LONG", "SHORT"}
    assert len(symbols) == len(files)
    assert len(timeframes) >= 3
    assert REQUIRED_STATES <= states


def test_source_implements_protocol_and_splits_open_setups(source: MockFixtureSource) -> None:
    assert isinstance(source, CciMissionSource)
    setups = source.list_setups()
    assert len(setups) >= 8
    open_setups = source.list_open_setups()
    assert open_setups
    assert all(not setup.resolved for setup in open_setups)
    assert any(setup.resolved for setup in setups)
    assert {setup.cci_setup_id for setup in open_setups}.isdisjoint(
        setup.cci_setup_id for setup in setups if setup.resolved
    )


def test_get_setup_unknown_id(source: MockFixtureSource) -> None:
    with pytest.raises(SetupNotFoundError):
        source.get_setup("missing_setup")


def test_iter_events_since_returns_fixture_events_in_order(source: MockFixtureSource) -> None:
    events = list(source.iter_events_since(None))
    assert events
    assert events == sorted(events, key=lambda event: (event.occurred_at, event.cci_event_id))

    cursor = events[3].cci_event_id
    after_id = list(source.iter_events_since(cursor))
    assert [event.cci_event_id for event in after_id] == [event.cci_event_id for event in events[4:]]

    stamp = events[0].occurred_at
    after_time = list(source.iter_events_since(stamp))
    assert all(event.occurred_at > stamp for event in after_time)
    assert events[0].cci_event_id not in {event.cci_event_id for event in after_time}


def test_rejects_fixture_that_drops_lifecycle(tmp_path: Path) -> None:
    payload = {
        "cci_setup_id": "mock_setup_bad",
        "symbol": "BTCUSDT",
        "timeframe": "H1",
        "direction": "LONG",
        "quality_tier": "STANDARD",
        "title": "Broken",
        "thesis_summary": "Missing events.",
        "disclaimer": "Synthetic.",
        "synthetic": True,
        "evidence": [{"type": "structure", "label": "None"}],
        "lifecycle": [],
    }
    (tmp_path / "bad.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FixtureContractError, match="lifecycle"):
        MockFixtureSource(tmp_path)


def test_refuses_live_cci_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCI_SOURCE", "live")
    with pytest.raises(RuntimeError, match="mock"):
        create_app()
