"""Process-only quests and discipline achievements.

Templates never ask for a trade, a profit, leverage, or a win rate.
Amounts are code-owned. A database row cannot raise them.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

BANNED_REWARD = re.compile(
    r"take\s+\d+\s+trades|most\s+trades|win[-\s]?rate|highest\s+pnl|\bleverage\b|\bpnl\b|profit\s+multiplier",
    re.IGNORECASE,
)

DEFAULT_PREFS = {
    "new_mission": True,
    "lifecycle_resolution": True,
    "quest_complete": False,
    "streak": False,
    "replay_nudge": False,
}


@dataclass(frozen=True)
class QuestTemplate:
    code: str
    cadence: str
    title: str
    detail: str
    xp: int
    metric: str
    target: int
    href: str


DAILY_QUESTS = (
    QuestTemplate("read-lock", "daily", "Read a mission and seal a call", "Any of the four decisions counts. A pass counts too.", 40, "locks", 1, "/missions"),
    QuestTemplate("no-trade", "daily", "Pass on purpose", "Seal NO TRADE. Passing is Pack strength.", 40, "no_trade", 1, "/missions"),
    QuestTemplate("journal", "daily", "Write the den journal", "Your notes stay separate from the CCI outcome.", 40, "journals", 1, "/missions"),
    QuestTemplate("replay", "daily", "Run one closed tape", "Train on a closed setup. The outcome stays masked until you reveal.", 40, "replays", 1, "/replay"),
    QuestTemplate("evidence", "daily", "Read the evidence all the way", "Mark the evidence read on one Mission.", 40, "evidence", 1, "/missions"),
)

WEEKLY_QUESTS = (
    QuestTemplate("journals-week", "weekly", "Three journals this week", "Three self-reports. They stay user-reported.", 100, "journals", 3, "/missions"),
    QuestTemplate("passes-week", "weekly", "Two NO TRADE locks", "Two deliberate passes. Passing is Pack strength.", 100, "no_trade", 2, "/missions"),
    QuestTemplate("replay-week", "weekly", "Five replay attempts", "Five closed tapes. A drill score is not a profit record.", 100, "replays", 5, "/replay"),
    QuestTemplate("streak-week", "weekly", "Keep a discipline streak of 5 days", "A lock, a journal, or a Replay. Not a trade streak.", 100, "streak", 5, "/profile"),
)

QUESTS = {quest.code: quest for quest in (*DAILY_QUESTS, *WEEKLY_QUESTS)}


@dataclass(frozen=True)
class AchievementTemplate:
    code: str
    name: str
    category: str
    rarity: str
    rule: str
    xp: int


ACHIEVEMENTS = (
    AchievementTemplate("A01", "First Lock", "Process", "Common", "Lock any decision once", 50),
    AchievementTemplate("A02", "Evidence Reader", "Process", "Common", "Full evidence read ×5", 50),
    AchievementTemplate("A03", "Journal Ink", "Review", "Common", "3 journals", 50),
    AchievementTemplate("A04", "Clean Pass", "Restraint", "Rare", "NO TRADE ×10", 100),
    AchievementTemplate("A05", "Patient Scout", "Restraint", "Rare", "NO TRADE on a HUNT-tier Mission", 100),
    AchievementTemplate("A06", "Review Ritual", "Review", "Rare", "Review checklist ×10", 100),
    AchievementTemplate("A07", "Replay Initiate", "Replay", "Common", "Complete 5 Replays", 50),
    AchievementTemplate("A08", "Pattern Eye", "Replay", "Rare", "≥80% score ×5 Replays", 100),
    AchievementTemplate("A09", "Quiet Week", "Restraint", "Epic", "Full week with a process action each day and no I TOOK THIS", 150),
    AchievementTemplate("A10", "Streak Seven", "Belonging", "Rare", "Discipline Streak 7", 100),
    AchievementTemplate("A11", "Streak Thirty", "Belonging", "Epic", "Discipline Streak 30", 150),
    AchievementTemplate("A12", "Pathwalker", "Process", "Rare", "Reach PATHFINDER", 100),
    AchievementTemplate("A13", "Vanguard Seal", "Belonging", "Epic", "Reach VANGUARD", 150),
    AchievementTemplate("A14", "Elite Howl", "Belonging", "Legendary", "Reach ELITE", 150),
    AchievementTemplate("A15", "Outcome Separatist", "Review", "Rare", "Journal plus CCI outcome on 5 Missions", 100),
    AchievementTemplate("A16", "No Chase", "Restraint", "Epic", "After an INVALIDATED Mission, pass or watch", 150),
    AchievementTemplate("A17", "Vault Dweller", "Replay", "Rare", "25 Replay completions", 100),
    AchievementTemplate("A18", "Pack Oath", "Belonging", "Common", "Onboarding plus first lock", 50),
)

ACHIEVEMENT_BY_CODE = {item.code: item for item in ACHIEVEMENTS}


def templates_are_process_only() -> list[str]:
    """Return any quest or achievement line that matches a banned reward."""
    hits: list[str] = []
    for quest in QUESTS.values():
        blob = f"{quest.code} {quest.title} {quest.detail}"
        if BANNED_REWARD.search(blob):
            hits.append(quest.code)
    for item in ACHIEVEMENTS:
        blob = f"{item.code} {item.name} {item.rule}"
        if BANNED_REWARD.search(blob):
            hits.append(item.code)
    return hits


def _hash(value: str) -> int:
    h = 2166136261
    for char in value:
        h ^= ord(char)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def pick_codes(pool: tuple[QuestTemplate, ...], count: int, seed_key: str) -> list[str]:
    items = [quest.code for quest in pool]
    picked: list[str] = []
    seed = _hash(seed_key)
    while len(picked) < count and items:
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        index = seed % len(items)
        picked.append(items.pop(index))
    return picked


def daily_period(now: datetime) -> tuple[str, datetime, datetime]:
    current = now.astimezone(timezone.utc)
    start = datetime.combine(current.date(), time.min, tzinfo=timezone.utc)
    return current.date().isoformat(), start, start + timedelta(days=1)


def weekly_period(now: datetime) -> tuple[str, datetime, datetime]:
    current = now.astimezone(timezone.utc).date()
    start_day = current - timedelta(days=current.weekday())
    start = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    iso = start_day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}", start, start + timedelta(days=7)


def surfaced_quests(user_key: str, now: datetime) -> list[tuple[QuestTemplate, str]]:
    day_key, _, _ = daily_period(now)
    week_key, _, _ = weekly_period(now)
    daily = [QUESTS[code] for code in pick_codes(DAILY_QUESTS, 3, f"{user_key}:{day_key}")]
    weekly = [QUESTS[code] for code in pick_codes(WEEKLY_QUESTS, 2, f"{user_key}:{week_key}")]
    return [(quest, day_key) for quest in daily] + [(quest, week_key) for quest in weekly]


def normalize_prefs(raw: dict | None) -> dict[str, bool]:
    prefs = dict(DEFAULT_PREFS)
    if not isinstance(raw, dict):
        return prefs
    for key in DEFAULT_PREFS:
        if isinstance(raw.get(key), bool):
            prefs[key] = raw[key]
    return prefs


def quiet_week(process_days: dict[date, set[str]], today: date) -> bool:
    for offset in range(7):
        kinds = process_days.get(today - timedelta(days=offset), set())
        if "process" not in kinds or "I_TOOK_THIS" in kinds:
            return False
    return True
