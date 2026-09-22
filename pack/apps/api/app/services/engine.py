"""Settle surfaced quests and discipline achievements.

Called from quest and achievement reads, not from the decision lock response.
Repeated calls award nothing more: completion rows and ledger keys are unique.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Achievement,
    Journal,
    Mission,
    ProcessMark,
    Quest,
    QuestCompletion,
    ReplayAttempt,
    User,
    UserAchievement,
    UserMissionDecision,
)
from app.domain.board import (
    ACHIEVEMENT_BY_CODE,
    QUESTS,
    daily_period,
    quiet_week,
    surfaced_quests,
    weekly_period,
)
from app.domain.progression import rank_name
from app.services.ledger import award


def _count(session: Session, stmt) -> int:
    return int(session.scalar(stmt) or 0)


def _window_counts(session: Session, user: User, start: datetime, end: datetime) -> dict[str, int]:
    return {
        "locks": _count(
            session,
            select(func.count(UserMissionDecision.id)).where(
                UserMissionDecision.user_id == user.id,
                UserMissionDecision.locked_at >= start,
                UserMissionDecision.locked_at < end,
            ),
        ),
        "no_trade": _count(
            session,
            select(func.count(UserMissionDecision.id)).where(
                UserMissionDecision.user_id == user.id,
                UserMissionDecision.decision == "NO_TRADE",
                UserMissionDecision.locked_at >= start,
                UserMissionDecision.locked_at < end,
            ),
        ),
        "journals": _count(
            session,
            select(func.count(Journal.id)).where(
                Journal.user_id == user.id,
                Journal.created_at >= start,
                Journal.created_at < end,
            ),
        ),
        "replays": _count(
            session,
            select(func.count(ReplayAttempt.id)).where(
                ReplayAttempt.user_id == user.id,
                ReplayAttempt.created_at >= start,
                ReplayAttempt.created_at < end,
            ),
        ),
        "evidence": _count(
            session,
            select(func.count(ProcessMark.id)).where(
                ProcessMark.user_id == user.id,
                ProcessMark.kind == "evidence",
                ProcessMark.created_at >= start,
                ProcessMark.created_at < end,
            ),
        ),
    }


def _metric(code: str, counts: dict[str, int], streak: int) -> int:
    quest = QUESTS[code]
    if quest.metric == "streak":
        return streak
    return int(counts.get(quest.metric, 0))


def _award_quest(session: Session, user: User, quest_row: Quest, period_key: str, now: datetime) -> int:
    existing = session.scalar(
        select(QuestCompletion).where(
            QuestCompletion.user_id == user.id,
            QuestCompletion.quest_id == quest_row.id,
            QuestCompletion.period_key == period_key,
        )
    )
    if existing is not None:
        return 0
    template = QUESTS[quest_row.code]
    row = QuestCompletion(user_id=user.id, quest_id=quest_row.id, period_key=period_key, completed_at=now)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return 0
    return award(
        session,
        user,
        raw_amount=template.xp,
        category="quest",
        reason_code=f"quest:{template.code}",
        idempotency_key=f"quest:{user.id}:{quest_row.id}:{period_key}",
        now=now,
        ref_type="quest_completion",
        ref_id=str(row.id),
    )


def _completed(session: Session, user: User, quest_row: Quest, period_key: str) -> bool:
    return (
        session.scalar(
            select(QuestCompletion.id).where(
                QuestCompletion.user_id == user.id,
                QuestCompletion.quest_id == quest_row.id,
                QuestCompletion.period_key == period_key,
            )
        )
        is not None
    )


def quest_board(session: Session, user: User, now: datetime) -> dict:
    day_key, day_start, day_end = daily_period(now)
    week_key, week_start, week_end = weekly_period(now)
    day_counts = _window_counts(session, user, day_start, day_end)
    week_counts = _window_counts(session, user, week_start, week_end)
    streak = int(user.discipline_streak or 0)
    rows = {row.code: row for row in session.scalars(select(Quest)).all()}
    daily = []
    weekly = []
    awarded = 0
    for template, period_key in surfaced_quests(str(user.id), now):
        quest_row = rows.get(template.code)
        counts = day_counts if template.cadence == "daily" else week_counts
        progress = _metric(template.code, counts, streak)
        if quest_row is not None and progress >= template.target:
            awarded += _award_quest(session, user, quest_row, period_key, now)
        done = quest_row is not None and _completed(session, user, quest_row, period_key)
        payload = {
            "code": template.code,
            "title": template.title,
            "detail": template.detail,
            "xp": template.xp,
            "period_key": period_key,
            "progress": min(progress, template.target),
            "target": template.target,
            "completed": done,
            "href": template.href,
            "cadence": template.cadence,
        }
        if template.cadence == "daily" and period_key == day_key:
            daily.append(payload)
        elif template.cadence == "weekly" and period_key == week_key:
            weekly.append(payload)
    return {"daily": daily, "weekly": weekly, "xp_awarded": awarded}


def _process_days(session: Session, user: User, today) -> dict:
    start = datetime.combine(today - timedelta(days=6), datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    days: dict = {}

    def mark(when: datetime, *kinds: str) -> None:
        key = when.astimezone(timezone.utc).date()
        bucket = days.setdefault(key, set())
        bucket.add("process")
        bucket.update(kinds)

    decisions = session.execute(
        select(UserMissionDecision.locked_at, UserMissionDecision.decision).where(
            UserMissionDecision.user_id == user.id,
            UserMissionDecision.locked_at >= start,
            UserMissionDecision.locked_at < end,
        )
    ).all()
    for locked_at, decision in decisions:
        mark(locked_at, decision)
    for column in (
        session.scalars(
            select(Journal.created_at).where(
                Journal.user_id == user.id, Journal.created_at >= start, Journal.created_at < end
            )
        ).all(),
        session.scalars(
            select(ReplayAttempt.created_at).where(
                ReplayAttempt.user_id == user.id,
                ReplayAttempt.created_at >= start,
                ReplayAttempt.created_at < end,
            )
        ).all(),
        session.scalars(
            select(ProcessMark.created_at).where(
                ProcessMark.user_id == user.id,
                ProcessMark.created_at >= start,
                ProcessMark.created_at < end,
            )
        ).all(),
    ):
        for created_at in column:
            mark(created_at)
    return days


def _stats(session: Session, user: User, now: datetime) -> dict:
    locks = _count(session, select(func.count(UserMissionDecision.id)).where(UserMissionDecision.user_id == user.id))
    no_trade = _count(
        session,
        select(func.count(UserMissionDecision.id)).where(
            UserMissionDecision.user_id == user.id, UserMissionDecision.decision == "NO_TRADE"
        ),
    )
    no_trade_hunt = _count(
        session,
        select(func.count(UserMissionDecision.id))
        .join(Mission, Mission.id == UserMissionDecision.mission_id)
        .where(
            UserMissionDecision.user_id == user.id,
            UserMissionDecision.decision == "NO_TRADE",
            Mission.quality_tier == "HUNT",
        ),
    )
    journals = _count(session, select(func.count(Journal.id)).where(Journal.user_id == user.id))
    evidence = _count(
        session,
        select(func.count(ProcessMark.id)).where(ProcessMark.user_id == user.id, ProcessMark.kind == "evidence"),
    )
    reviews = _count(
        session,
        select(func.count(ProcessMark.id)).where(ProcessMark.user_id == user.id, ProcessMark.kind == "review"),
    )
    replays = _count(session, select(func.count(ReplayAttempt.id)).where(ReplayAttempt.user_id == user.id))
    high_scores = _count(
        session,
        select(func.count(ReplayAttempt.id)).where(ReplayAttempt.user_id == user.id, ReplayAttempt.score >= 80),
    )
    outcome_journals = _count(
        session,
        select(func.count(Journal.id))
        .join(Mission, Mission.id == Journal.mission_id)
        .where(Journal.user_id == user.id, Mission.outcome_code.is_not(None)),
    )
    no_chase = _count(
        session,
        select(func.count(UserMissionDecision.id))
        .join(Mission, Mission.id == UserMissionDecision.mission_id)
        .where(
            UserMissionDecision.user_id == user.id,
            UserMissionDecision.decision.in_(("NO_TRADE", "WATCH_ONLY")),
            Mission.lifecycle_state == "INVALIDATED",
        ),
    )
    xp = int(user.pack_xp or 0)
    today = now.astimezone(timezone.utc).date()
    return {
        "A01": locks >= 1,
        "A02": evidence >= 5,
        "A03": journals >= 3,
        "A04": no_trade >= 10,
        "A05": no_trade_hunt >= 1,
        "A06": reviews >= 10,
        "A07": replays >= 5,
        "A08": high_scores >= 5,
        "A09": quiet_week(_process_days(session, user, today), today),
        "A10": int(user.discipline_streak or 0) >= 7,
        "A11": int(user.discipline_streak or 0) >= 30,
        "A12": xp >= 1800,
        "A13": xp >= 4000,
        "A14": xp >= 8500,
        "A15": outcome_journals >= 5,
        "A16": no_chase >= 1,
        "A17": replays >= 25,
        "A18": user.oath_accepted_at is not None and locks >= 1,
    }


def _award_achievement(session: Session, user: User, code: str, now: datetime) -> int:
    template = ACHIEVEMENT_BY_CODE[code]
    achievement = session.scalar(select(Achievement).where(Achievement.code == code))
    if achievement is None:
        return 0
    existing = session.scalar(
        select(UserAchievement).where(
            UserAchievement.user_id == user.id,
            UserAchievement.achievement_id == achievement.id,
        )
    )
    if existing is not None:
        return 0
    row = UserAchievement(user_id=user.id, achievement_id=achievement.id, unlocked_at=now)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return 0
    return award(
        session,
        user,
        raw_amount=template.xp,
        category="achievement",
        reason_code=f"achievement:{code}",
        idempotency_key=f"achievement:{user.id}:{code}",
        now=now,
        ref_type="user_achievement",
        ref_id=str(row.id),
    )


def settle_achievements(session: Session, user: User, now: datetime) -> int:
    awarded = 0
    # Rank checks read pack_xp, so quest XP must already be applied.
    for code, ready in _stats(session, user, now).items():
        if ready:
            awarded += _award_achievement(session, user, code, now)
    user.wolf_rank = rank_name(int(user.pack_xp or 0))
    return awarded


def settle_progression(session: Session, user: User, now: datetime) -> dict:
    board = quest_board(session, user, now)
    achievement_xp = settle_achievements(session, user, now)
    return {
        "daily": board["daily"],
        "weekly": board["weekly"],
        "xp_awarded": board["xp_awarded"] + achievement_xp,
    }


def unlocked_codes(session: Session, user: User) -> list[str]:
    rows = session.execute(
        select(Achievement.code)
        .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
        .where(UserAchievement.user_id == user.id)
        .order_by(Achievement.code)
    ).all()
    return [code for (code,) in rows]
