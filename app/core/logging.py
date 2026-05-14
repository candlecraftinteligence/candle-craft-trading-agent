from __future__ import annotations

import logging.config


def configure_logging(log_level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {
                "level": log_level,
                "handlers": ["console"],
            },
            "loggers": {
                "uvicorn.access": {
                    "level": log_level,
                    "handlers": ["console"],
                    "propagate": False,
                }
            },
        }
    )
