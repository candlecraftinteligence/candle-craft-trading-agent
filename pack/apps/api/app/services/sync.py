from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Achievement, Mission, MissionLifecycleEvent, Quest, ReplayChallenge
from app.integrations.cci.protocol import CciMissionSource

ACHIEVEMENTS = (
    ("A01", "First Lock", "Process", "Common", 50),
    ("A02", "Evidence Reader", "Process", "Common", 50),
    ("A03", "Journal Ink", "Review", "Common", 50),
    ("A04", "Clean Pass", "Restraint", "Rare", 100),
    ("A05", "Patient Scout", "Restraint", "Rare", 100),
    ("A06", "Review Ritual", "Review", "Rare", 100),
    ("A07", "Replay Initiate", "Replay", "Common", 50),
    ("A08", "Pattern Eye", "Replay", "Rare", 100),
    ("A09", "Quiet Week", "Restraint", "Epic", 150),
    ("A10", "Streak Seven", "Belonging", "Rare", 100),
    ("A11", "Streak Thirty", "Belonging", "Epic", 150),
    ("A12", "Pathwalker", "Process", "Rare", 100),
    ("A13", "Vanguard Seal", "Belonging", "Epic", 150),
    ("A14", "Elite Howl", "Belonging", "Legendary", 150),
    ("A15", "Outcome Separatist", "Review", "Rare", 100),
    ("A16", "No Chase", "Restraint", "Epic", 150),
    ("A17", "Vault Dweller", "Replay", "Rare", 100),
    ("A18", "Pack Oath", "Belonging", "Common", 50),
)

QUESTS = (
    ("read-lock", "daily", "Read a mission and seal a call", 40),
    ("no-trade", "daily", "Pass on purpose", 40),
    ("journal", "daily", "Write the den journal", 40),
    ("replay", "daily", "Run one closed tape", 40),
    ("evidence", "daily", "Read the evidence all the way", 40),
    ("journals-week", "weekly", "Three journals this week", 100),
    ("passes-week", "weekly", "Two NO TRADE locks", 100),
    ("replay-week", "weekly", "Five replay attempts", 100),
)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed_reference(session: Session) -> None:
    for code, name, category, rarity, xp in ACHIEVEMENTS:
        row = session.scalar(select(Achievement).where(Achievement.code == code))
        if row is None:
            session.add(
                Achievement(
                    code=code,
                    name=name,
                    category=category,
                    rarity=rarity,
                    rule_json={"code": code},
                    xp_reward=xp,
                )
            )
    for code, cadence, title, xp in QUESTS:
        row = session.scalar(select(Quest).where(Quest.code == code))
        if row is None:
            session.add(
                Quest(
                    code=code,
                    cadence=cadence,
                    title=title,
                    rule_json={"code": code},
                    xp_reward=xp,
                    active=True,
                )
            )
    session.flush()


def sync_missions(session: Session, source: CciMissionSource) -> None:
    setups = source.list_setups() if hasattr(source, "list_setups") else source.list_open_setups()
    for setup in setups:
        mission = session.scalar(select(Mission).where(Mission.cci_setup_id == setup.cci_setup_id))
        if mission is None:
            opened = _parse_dt(setup.opened_at) or datetime.now(timezone.utc)
            mission = Mission(cci_setup_id=setup.cci_setup_id, opened_at=opened)
            session.add(mission)
        mission.symbol = setup.symbol
        mission.timeframe = setup.timeframe
        mission.direction = setup.direction
        mission.quality_tier = setup.quality_tier
        mission.title = setup.title
        mission.thesis_summary = setup.thesis_summary
        mission.evidence_json = list(setup.evidence)
        mission.lifecycle_state = setup.lifecycle_state
        mission.opened_at = _parse_dt(setup.opened_at) or datetime.now(timezone.utc)
        mission.resolved_at = _parse_dt(setup.resolved_at)
        mission.outcome_code = setup.outcome_code
        mission.outcome_payload_json = None if setup.outcome_code is None else {"outcome_code": setup.outcome_code}
        mission.source = "mock"
        mission.synthetic = bool(setup.synthetic)
        mission.disclaimer = setup.disclaimer
        session.flush()
        for event in setup.lifecycle:
            event_id = event["cci_event_id"]
            existing = session.scalar(
                select(MissionLifecycleEvent).where(MissionLifecycleEvent.cci_event_id == event_id)
            )
            if existing is not None:
                continue
            session.add(
                MissionLifecycleEvent(
                    mission_id=mission.id,
                    cci_event_id=event_id,
                    event_type=event["event_type"],
                    state=event["state"],
                    payload_json=event.get("payload") or {"outcome_code": event.get("outcome_code")},
                    occurred_at=_parse_dt(event["occurred_at"]) or mission.opened_at,
                )
            )
        replay = setup.replay if isinstance(setup.replay, dict) else None
        if setup.resolved and replay:
            challenge = session.scalar(
                select(ReplayChallenge).where(ReplayChallenge.source_mission_id == mission.id)
            )
            rubric = {
                "preferred_decision": replay.get("preferred_decision"),
                "quality_tier": setup.quality_tier,
            }
            if challenge is None:
                session.add(
                    ReplayChallenge(
                        source_mission_id=mission.id,
                        fixture_json={
                            "masked_title": replay.get("masked_title"),
                            "masked_thesis": replay.get("masked_thesis"),
                            "evidence": replay.get("evidence") or [],
                        },
                        teaching_note=str(replay.get("teaching_note") or ""),
                        rubric_json=rubric,
                        active=True,
                    )
                )
            else:
                challenge.rubric_json = rubric
                challenge.teaching_note = str(replay.get("teaching_note") or "")
    session.flush()
