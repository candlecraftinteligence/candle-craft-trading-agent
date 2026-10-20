"""Output paths must stay inside an explicit DEV research root."""

from __future__ import annotations

import os
from pathlib import Path

from research.market_data_foundation.constants import LIVE_DATABASE_NAME, RUNTIME_CHECKOUT_SEGMENT


class PathGuardError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_research_root() -> Path:
    return (repository_root() / "research_runs" / "market_data_foundation").resolve(strict=False)


def resolve_research_root(path: Path | str | None) -> Path:
    candidate = Path(path).expanduser() if path is not None else default_research_root()
    if not candidate.is_absolute():
        candidate = repository_root() / candidate
    resolved = candidate.resolve(strict=False)
    _reject_runtime(resolved)
    return resolved


def resolve_output(root: Path | str, relative: str) -> Path:
    text = str(relative).strip()
    if not text:
        raise PathGuardError("relative_path_required")
    raw = Path(text)
    if raw.is_absolute():
        raise PathGuardError("absolute_output_rejected")
    if any(part in {"", ".", ".."} and part == ".." for part in raw.parts) or ".." in raw.parts:
        raise PathGuardError("traversal_rejected")
    research_root = resolve_research_root(root)
    candidate = (research_root / raw).resolve(strict=False)
    try:
        candidate.relative_to(research_root)
    except ValueError as exc:
        raise PathGuardError("escaped_research_root") from exc
    _reject_runtime(candidate)
    return candidate


def _reject_runtime(path: Path) -> None:
    name = path.name.casefold()
    forbidden = {
        LIVE_DATABASE_NAME,
        f"{LIVE_DATABASE_NAME}-wal",
        f"{LIVE_DATABASE_NAME}-shm",
    }
    if name in forbidden:
        raise PathGuardError("live_runtime_database_rejected")
    segments = [part.casefold() for part in path.parts]
    if RUNTIME_CHECKOUT_SEGMENT in segments:
        raise PathGuardError("runtime_checkout_rejected")
    if os.path.normcase(path.name).casefold() in forbidden:
        raise PathGuardError("live_runtime_database_rejected")
