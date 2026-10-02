"""Telegram webhook stub.

Live delivery stays off until PACK_WEBHOOK_SECRET and PACK_TELEGRAM_BOT_TOKEN are set.
A missing secret does not boot-fail the API. A present secret must match the header.
"""

import hmac
import os

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import client_host, limit

router = APIRouter()


def webhook_secret_matches(header: str | None, expected: str) -> bool:
    if not expected or header is None:
        return False
    try:
        return hmac.compare_digest(header, expected)
    except (TypeError, ValueError):
        return False


@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    limit(f"webhook:{client_host(request)}", 60, 60)
    expected = os.environ.get("PACK_WEBHOOK_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured.")
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not webhook_secret_matches(header, expected):
        raise HTTPException(status_code=401, detail="Webhook secret was rejected.")
    return {"ok": True, "handled": False}
