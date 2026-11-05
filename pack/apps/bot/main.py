"""Run The Pack bot. Uses PACK_TELEGRAM_BOT_TOKEN only."""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1] / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.bot.runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
