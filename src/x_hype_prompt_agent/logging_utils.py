from __future__ import annotations

import logging
import os
from typing import Any


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        token = os.getenv("TELEGRAM_X_HYPE_BOT_TOKEN")
        if token:
            record.msg = _redact(record.msg, token)
            if record.args:
                record.args = tuple(_redact(arg, token) for arg in record.args)
        return True


def configure_logging(level: str = "INFO") -> None:
    normalized_level = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=normalized_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger().addFilter(SecretRedactionFilter())


def _redact(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    return value
