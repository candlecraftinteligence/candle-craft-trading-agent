"""Pack bot process. Disabled when PACK_TELEGRAM_BOT_TOKEN is unset.

This token is The Pack's own BotFather token. It is never the CCI production bot.
"""

import os


def _flag(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def pack_bot_token() -> str:
    return os.environ.get("PACK_TELEGRAM_BOT_TOKEN", "").strip()


def ensure_bot_optional() -> str:
    """API boot stays up without a token unless BOT_REQUIRED=true."""
    if _flag("BOT_REQUIRED") and not pack_bot_token():
        raise RuntimeError("BOT_REQUIRED is true and PACK_TELEGRAM_BOT_TOKEN is unset.")
    return "enabled" if pack_bot_token() else "disabled"


def main() -> int:
    token = pack_bot_token()
    if not token:
        if _flag("BOT_REQUIRED"):
            raise SystemExit("PACK_TELEGRAM_BOT_TOKEN is required.")
        print("Pack bot disabled: PACK_TELEGRAM_BOT_TOKEN is unset. The API keeps running.")
        return 0

    from aiogram import Bot, Dispatcher
    from aiogram.filters import CommandStart
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

    from app.bot.messages import start_reply

    async def on_start(message: Message) -> None:
        payload = None
        text = message.text or ""
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1]
        reply = start_reply(os.environ.get("PACK_MINI_APP_URL", ""), payload)
        markup = None
        if reply["web_app_url"]:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=reply["button_text"],
                            web_app=WebAppInfo(url=reply["web_app_url"]),
                        )
                    ]
                ]
            )
        await message.answer(reply["text"], reply_markup=markup)

    dispatcher = Dispatcher()
    dispatcher.message.register(on_start, CommandStart())
    import asyncio

    asyncio.run(dispatcher.start_polling(Bot(token=token)))
    return 0
