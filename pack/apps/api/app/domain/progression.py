"""Server-owned Pack XP, caps, streak, and Wolf Rank.

Amounts match the product architecture. The client never supplies an amount.
"""

from datetime import datetime, timedelta, timezone

DECISION_XP = {
    "NO_TRADE": 25,
    "I_TOOK_THIS": 20,
    "TRACK": 15,
    "WATCH_ONLY": 15,
}

JOURNAL_XP = 30
REPLAY_COMPLETE_XP = 15
REPLAY_HIGH_XP = 25
REPLAY_HIGH_SCORE = 80

DAILY_CAPS = {
    "decision": 80,
    "journal_review": 100,
    "replay": 90,
    "quest": 140,
}

WOLF_RANKS = (
    ("SCOUT", 0),
    ("TRACKER", 250),
    ("HUNTER", 750),
    ("PATHFINDER", 1800),
    ("VANGUARD", 4000),
    ("ELITE", 8500),
)

STREAK_BONUS = {3: 10, 7: 20, 14: 40}
STREAK_WEEKLY_PULSE = 10


def rank_name(xp: int) -> str:
    current = WOLF_RANKS[0][0]
    safe = max(0, int(xp))
    for name, threshold in WOLF_RANKS:
        if safe >= threshold:
            current = name
    return current


def apply_daily_cap(category: str, used_today: int, raw: int) -> int:
    """Clamp an award to the category cap. Past 70% of the cap, halve it."""
    if raw <= 0:
        return 0
    cap = DAILY_CAPS.get(category)
    if cap is None:
        return raw
    used = max(0, int(used_today))
    if used >= cap:
        return 0
    amount = int(raw)
    if used >= (cap * 70) // 100:
        amount = amount // 2
    room = cap - used
    return max(0, min(amount, room))


def replay_base_xp(attempts_already_today: int, score: int) -> int:
    """Completion XP plus a high-score bonus, then diminishing returns.

    Attempts 1–3 are full. 4–5 are halved. 6+ are quartered.
    """
    complete = REPLAY_COMPLETE_XP
    bonus = REPLAY_HIGH_XP if score >= REPLAY_HIGH_SCORE else 0
    base = complete + bonus
    attempt_number = attempts_already_today + 1
    if attempt_number > 5:
        return int(base * 0.25)
    if attempt_number > 3:
        return int(base * 0.5)
    return base


def score_replay(*, actual_tier: str, chosen_tier: str, preferred: str, chosen: str, evidence_reviewed: bool) -> int:
    quality = 40 if actual_tier == chosen_tier else 0
    decision = 40 if preferred == chosen else 0
    attention = 20 if evidence_reviewed else 0
    return quality + decision + attention


def next_streak(previous: int, last_process_at: datetime | None, now: datetime) -> int:
    """Discipline streak from process actions, with a 36h grace after a missed day."""
    if last_process_at is None or previous <= 0:
        return 1
    last = last_process_at.astimezone(timezone.utc)
    current = now.astimezone(timezone.utc)
    if last.date() == current.date():
        return previous
    if last.date() == current.date() - timedelta(days=1):
        return previous + 1
    if current - last <= timedelta(hours=36):
        return previous
    return 1


def streak_bonus_for(new_streak: int, previous: int) -> int | None:
    if new_streak == previous:
        return None
    if new_streak in STREAK_BONUS:
        return STREAK_BONUS[new_streak]
    if new_streak > 14 and new_streak % 7 == 0:
        return STREAK_WEEKLY_PULSE
    return None
