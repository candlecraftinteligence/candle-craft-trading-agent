import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import User, XpLedger
from app.domain.progression import apply_daily_cap, next_streak, rank_name, streak_bonus_for


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    current = now.astimezone(timezone.utc)
    start = datetime.combine(current.date(), time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def used_today(session: Session, user_id: uuid.UUID, category: str, now: datetime) -> int:
    start, end = _day_bounds(now)
    total = session.scalar(
        select(func.coalesce(func.sum(XpLedger.amount), 0)).where(
            XpLedger.user_id == user_id,
            XpLedger.category == category,
            XpLedger.created_at >= start,
            XpLedger.created_at < end,
        )
    )
    return int(total or 0)


def award(
    session: Session,
    user: User,
    *,
    raw_amount: int,
    category: str,
    reason_code: str,
    idempotency_key: str,
    now: datetime,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> int:
    """Append one ledger row. A repeated key awards nothing more."""
    existing = session.scalar(select(XpLedger).where(XpLedger.idempotency_key == idempotency_key))
    if existing is not None:
        return 0
    granted = apply_daily_cap(category, used_today(session, user.id, category, now), raw_amount)
    session.add(
        XpLedger(
            user_id=user.id,
            amount=granted,
            category=category,
            reason_code=reason_code,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=idempotency_key,
            created_at=now,
        )
    )
    user.pack_xp = int(user.pack_xp) + granted
    user.wolf_rank = rank_name(user.pack_xp)
    session.flush()
    return granted


def touch_streak(session: Session, user: User, now: datetime) -> None:
    previous = int(user.discipline_streak or 0)
    updated = next_streak(previous, user.last_process_at, now)
    user.discipline_streak = updated
    user.last_process_at = now
    user.streak_updated_on = now.astimezone(timezone.utc).date()
    bonus = streak_bonus_for(updated, previous)
    if bonus is None:
        return
    award(
        session,
        user,
        raw_amount=bonus,
        category="streak",
        reason_code=f"streak:{updated}",
        idempotency_key=f"streak:{user.id}:{updated}",
        now=now,
        ref_type="user",
        ref_id=str(user.id),
    )
