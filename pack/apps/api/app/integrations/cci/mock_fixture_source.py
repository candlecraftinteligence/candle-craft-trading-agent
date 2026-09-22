from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.integrations.cci.dto import CciEventDTO, CciSetupDTO
from app.integrations.cci.errors import FixtureContractError, SetupNotFoundError

EVENT_TYPES = frozenset({"SETUP_PUBLISHED", "LIFECYCLE_UPDATED", "SETUP_RESOLVED"})
QUALITY_TIERS = frozenset({"STANDARD", "HUNT"})
DIRECTIONS = frozenset({"LONG", "SHORT"})
RESOLVED_EVENT = "SETUP_RESOLVED"
TRAINING_DECISIONS = frozenset({"TRACK", "TAKE", "WATCH", "NO_TRADE"})
MASKED_LEAKS = (
    "tp_hit",
    "tp1",
    "tp2",
    "sl_hit",
    "invalidated",
    "expired",
    "outcome",
    "closed",
    "hunt",
    "standard",
)


def default_pack_fixtures_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "fixtures" / "missions"
        if candidate.is_dir() and (parent / "apps" / "api").is_dir():
            return candidate
    raise FixtureContractError("pack/fixtures/missions was not found beside the API package.")


def _parse_occurred_at(value: str, *, path: Path, event_id: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"{path.name}: {event_id} is missing occurred_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FixtureContractError(f"{path.name}: {event_id} has an unreadable occurred_at") from exc
    if parsed.tzinfo is None:
        raise FixtureContractError(f"{path.name}: {event_id} occurred_at must include a timezone")
    return parsed


def _require_text(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"{path.name}: {key} must be a non-empty string")
    return value


def _validate_raw(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise FixtureContractError(f"{path.name}: fixture root must be an object")

    cci_setup_id = _require_text(data, "cci_setup_id", path)
    _require_text(data, "symbol", path)
    _require_text(data, "timeframe", path)
    direction = _require_text(data, "direction", path)
    quality_tier = _require_text(data, "quality_tier", path)
    _require_text(data, "title", path)
    _require_text(data, "thesis_summary", path)
    _require_text(data, "disclaimer", path)

    if direction not in DIRECTIONS:
        raise FixtureContractError(f"{path.name}: direction must be LONG or SHORT")
    if quality_tier not in QUALITY_TIERS:
        raise FixtureContractError(f"{path.name}: quality_tier must be STANDARD or HUNT")
    if data.get("synthetic") is not True:
        raise FixtureContractError(f"{path.name}: synthetic must be true")
    if "hunt" in data or "hunts" in data:
        raise FixtureContractError(f"{path.name}: Hunt is a quality tier, not a separate entity")

    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise FixtureContractError(f"{path.name}: evidence must be a non-empty list")
    for index, block in enumerate(evidence):
        if not isinstance(block, dict):
            raise FixtureContractError(f"{path.name}: evidence[{index}] must be an object")
        if not isinstance(block.get("type"), str) or not block["type"].strip():
            raise FixtureContractError(f"{path.name}: evidence[{index}].type is required")
        if not isinstance(block.get("label"), str) or not block["label"].strip():
            raise FixtureContractError(f"{path.name}: evidence[{index}].label is required")

    lifecycle = data.get("lifecycle")
    if not isinstance(lifecycle, list) or not lifecycle:
        raise FixtureContractError(f"{path.name}: lifecycle must be a non-empty list")

    times: list[datetime] = []
    for index, event in enumerate(lifecycle):
        if not isinstance(event, dict):
            raise FixtureContractError(f"{path.name}: lifecycle[{index}] must be an object")
        event_id = event.get("cci_event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise FixtureContractError(f"{path.name}: lifecycle[{index}] is missing cci_event_id")
        event_type = event.get("event_type")
        if event_type not in EVENT_TYPES:
            raise FixtureContractError(f"{path.name}: {event_id} has an unknown event_type")
        state = event.get("state")
        if not isinstance(state, str) or not state.strip():
            raise FixtureContractError(f"{path.name}: {event_id} is missing state")
        times.append(_parse_occurred_at(event.get("occurred_at", ""), path=path, event_id=event_id))
        outcome = event.get("outcome_code")
        if outcome is not None and (not isinstance(outcome, str) or not outcome.strip()):
            raise FixtureContractError(f"{path.name}: {event_id} outcome_code must be a non-empty string")
        payload = event.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise FixtureContractError(f"{path.name}: {event_id} payload must be an object")

    if times != sorted(times):
        raise FixtureContractError(f"{path.name}: lifecycle events must be ordered by occurred_at")
    if lifecycle[0].get("event_type") != "SETUP_PUBLISHED":
        raise FixtureContractError(f"{cci_setup_id}: the first event must be SETUP_PUBLISHED")

    resolved_at = [i for i, event in enumerate(lifecycle) if event.get("event_type") == RESOLVED_EVENT]
    if len(resolved_at) > 1:
        raise FixtureContractError(f"{cci_setup_id}: only one SETUP_RESOLVED event is allowed")
    if resolved_at and resolved_at[0] != len(lifecycle) - 1:
        raise FixtureContractError(f"{cci_setup_id}: SETUP_RESOLVED must be the last event")

    _validate_replay(data, path, resolved=bool(resolved_at))
    return data


def _validate_replay(data: dict[str, Any], path: Path, *, resolved: bool) -> None:
    replay = data.get("replay")
    if not resolved:
        if replay is not None:
            raise FixtureContractError(f"{path.name}: open fixtures must not include a replay brief")
        return
    if not isinstance(replay, dict):
        raise FixtureContractError(f"{path.name}: resolved fixtures require a replay brief")

    for key in ("masked_title", "masked_thesis", "teaching_note"):
        value = replay.get(key)
        if not isinstance(value, str) or not value.strip():
            raise FixtureContractError(f"{path.name}: replay.{key} must be a non-empty string")
    preferred = replay.get("preferred_decision")
    if preferred not in TRAINING_DECISIONS:
        raise FixtureContractError(f"{path.name}: replay.preferred_decision is not a training decision")

    evidence = replay.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise FixtureContractError(f"{path.name}: replay.evidence must be a non-empty list")
    parts = [replay["masked_title"], replay["masked_thesis"]]
    for index, block in enumerate(evidence):
        if not isinstance(block, dict):
            raise FixtureContractError(f"{path.name}: replay.evidence[{index}] must be an object")
        if not isinstance(block.get("type"), str) or not str(block["type"]).strip():
            raise FixtureContractError(f"{path.name}: replay.evidence[{index}].type is required")
        if not isinstance(block.get("label"), str) or not str(block["label"]).strip():
            raise FixtureContractError(f"{path.name}: replay.evidence[{index}].label is required")
        parts.append(str(block["label"]))
        if block.get("detail") is not None:
            parts.append(str(block["detail"]))
    masked = "\n".join(parts).lower()
    for leak in MASKED_LEAKS:
        if leak in masked:
            raise FixtureContractError(f"{path.name}: replay mask leaks '{leak}'")


def _outcome_code(lifecycle: list[dict[str, Any]]) -> str | None:
    found: str | None = None
    for event in lifecycle:
        code = event.get("outcome_code")
        if isinstance(code, str) and code:
            found = code
    return found


def _is_resolved(lifecycle: list[dict[str, Any]]) -> bool:
    return any(event.get("event_type") == RESOLVED_EVENT for event in lifecycle)


def project_setup(raw: dict[str, Any]) -> CciSetupDTO:
    lifecycle = raw["lifecycle"]
    resolved = _is_resolved(lifecycle)
    last = lifecycle[-1]
    return CciSetupDTO(
        cci_setup_id=raw["cci_setup_id"],
        symbol=raw["symbol"],
        timeframe=raw["timeframe"],
        direction=raw["direction"],
        quality_tier=raw["quality_tier"],
        title=raw["title"],
        thesis_summary=raw["thesis_summary"],
        evidence=raw["evidence"],
        lifecycle=lifecycle,
        lifecycle_state=last["state"],
        outcome_code=_outcome_code(lifecycle),
        opened_at=lifecycle[0]["occurred_at"],
        resolved_at=last["occurred_at"] if resolved else None,
        resolved=resolved,
        synthetic=True,
        disclaimer=raw["disclaimer"],
        replay=raw.get("replay"),
    )


class MockFixtureSource:
    """Loads synthetic missions from pack/fixtures/missions.

    Lifecycle arrays are stored and returned as parsed. This source does
    not insert, rename, or advance states.
    """

    source_name = "mock"

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        if fixtures_dir is not None:
            self.fixtures_dir = Path(fixtures_dir)
        else:
            override = os.environ.get("PACK_FIXTURES_DIR")
            self.fixtures_dir = Path(override) if override else default_pack_fixtures_dir()
        self._setups = self._load()
        self._by_id = {setup.cci_setup_id: setup for setup in self._setups}

    def _load(self) -> list[CciSetupDTO]:
        if not self.fixtures_dir.is_dir():
            raise FixtureContractError(f"Fixture directory does not exist: {self.fixtures_dir}")

        paths = sorted(self.fixtures_dir.glob("*.json"))
        if not paths:
            raise FixtureContractError(f"No mission fixtures in {self.fixtures_dir}")

        raw_setups: list[dict[str, Any]] = []
        seen_setups: set[str] = set()
        seen_events: set[str] = set()

        for path in paths:
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise FixtureContractError(f"{path.name}: invalid JSON") from exc
            raw = _validate_raw(parsed, path)
            setup_id = raw["cci_setup_id"]
            if setup_id in seen_setups:
                raise FixtureContractError(f"Duplicate cci_setup_id: {setup_id}")
            seen_setups.add(setup_id)
            for event in raw["lifecycle"]:
                event_id = event["cci_event_id"]
                if event_id in seen_events:
                    raise FixtureContractError(f"Duplicate cci_event_id: {event_id}")
                seen_events.add(event_id)
            raw_setups.append(raw)

        setups = [project_setup(raw) for raw in raw_setups]
        setups.sort(key=lambda setup: (setup.opened_at, setup.cci_setup_id), reverse=True)
        return setups

    def list_setups(self) -> list[CciSetupDTO]:
        return list(self._setups)

    def list_open_setups(self) -> list[CciSetupDTO]:
        return [setup for setup in self._setups if not setup.resolved]

    def get_setup(self, cci_setup_id: str) -> CciSetupDTO:
        try:
            return self._by_id[cci_setup_id]
        except KeyError as exc:
            raise SetupNotFoundError(cci_setup_id) from exc

    def iter_events_since(self, cursor: str | None) -> Iterable[CciEventDTO]:
        events: list[CciEventDTO] = []
        for setup in self._setups:
            for raw in setup.lifecycle:
                outcome = raw.get("outcome_code")
                payload = raw.get("payload")
                events.append(
                    CciEventDTO(
                        cci_event_id=raw["cci_event_id"],
                        cci_setup_id=setup.cci_setup_id,
                        event_type=raw["event_type"],
                        state=raw["state"],
                        occurred_at=raw["occurred_at"],
                        outcome_code=outcome if isinstance(outcome, str) else None,
                        payload=payload if isinstance(payload, dict) else None,
                    )
                )
        events.sort(key=lambda event: (_aware(event.occurred_at), event.cci_event_id))
        if cursor is None:
            return iter(events)

        ids = [event.cci_event_id for event in events]
        if cursor in ids:
            return iter(events[ids.index(cursor) + 1 :])

        return iter(event for event in events if event.occurred_at > cursor)


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
