"""Process-safety checks that never print a full environment."""

from __future__ import annotations

import os
from typing import Mapping

SAFE_TRUE = {"1", "true", "yes", "on"}
SAFE_FALSE = {"0", "false", "no", "off"}

PROCESS_FLAGS = (
    ("TELEGRAM_DRY_RUN", "true", SAFE_TRUE),
    ("TELEGRAM_SIGNALS_ENABLED", "false", SAFE_FALSE),
    ("LOCAL_MANUAL_MODE", "true", SAFE_TRUE),
    ("ORDER_EXECUTION_ENABLED", "false", SAFE_FALSE),
)


class UnsafeProcessConfiguration(RuntimeError):
    def __init__(self, flag: str) -> None:
        self.flag = flag
        super().__init__(f"unsafe_process_flag:{flag}")


def enforce_research_process(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Record the four DEV flags. Set an unset flag on this process only.

    An explicitly unsafe value aborts. The function never reads ``.env`` and
    never returns unrelated environment entries.
    """

    source = os.environ if environ is None else environ
    report: dict[str, str] = {}
    for name, safe_value, allowed in PROCESS_FLAGS:
        raw = source.get(name)
        if raw is None or str(raw).strip() == "":
            if environ is None:
                os.environ[name] = safe_value
            report[name] = "unset_then_safe_default_for_research_process"
            continue
        if str(raw).strip().lower() not in allowed:
            raise UnsafeProcessConfiguration(name)
        report[name] = "explicit_safe"
    return report
