"""Pack bot copy and notification filters.

No network. No CCI production token. Gamification notices stay off until the user opts in.
"""

import re

from app.domain.board import normalize_prefs

SAFE_MISSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
URGENCY = re.compile(
    r"hurry|last chance|don'?t miss|guaranteed|act now|take the trade|right now or",
    re.IGNORECASE,
)

WELCOME = (
    "The Pack is open.\n"
    "Profit is the outcome. Discipline is the game.\n"
    "Open the Mini App when you want the board. This chat does not repeat the whole den."
)

NOTICES = {
    "new_mission": "A mission is on the board. Open it when you are ready.",
    "lifecycle_resolution": "A fixture has a resolution. The outcome is in the Mini App.",
    "quest_complete": "A drill is logged. Pack XP is on your profile.",
    "streak": "Discipline streak: one quiet day is grace. A lock, journal, or Replay keeps it.",
    "replay_nudge": "The tape is quiet. Replay is open when you want to train.",
}


def start_reply(mini_app_url: str, mission_id: str | None = None) -> dict:
    base = (mini_app_url or "").strip().rstrip("/")
    mission = mission_id if mission_id and SAFE_MISSION_ID.fullmatch(mission_id) else None
    if not base:
        url = None
    elif mission:
        url = f"{base}/missions/{mission}"
    else:
        url = base
    return {
        "text": WELCOME,
        "button_text": "Open The Pack",
        "web_app_url": url,
        "mission_id": mission,
    }


def telegram_startapp_url(username: str, mission_id: str) -> str:
    handle = username.strip().lstrip("@")
    return f"https://t.me/{handle}/app?startapp={mission_id}"


def outbound_notice(prefs: dict | None, kind: str, mission_id: str | None = None) -> dict | None:
    """Return a short notice, or nothing when the user has that type off."""
    if kind not in NOTICES:
        return None
    if not normalize_prefs(prefs).get(kind, False):
        return None
    mission = mission_id if mission_id and SAFE_MISSION_ID.fullmatch(mission_id) else None
    return {"kind": kind, "text": NOTICES[kind], "mission_id": mission}


def copy_is_calm() -> list[str]:
    hits = []
    blobs = [WELCOME, *NOTICES.values()]
    for blob in blobs:
        if URGENCY.search(blob):
            hits.append(blob)
    return hits
