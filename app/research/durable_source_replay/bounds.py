"""Finite capture budgets for the durable source-replay evidence store.

These limits are part of the F04 contract. Exhausting one is a visible research
failure. It is not permission to queue, drop, or delete evidence without bound.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.research.durable_source_replay.constants import MAX_PARENT_DEPTH


@dataclass(frozen=True, slots=True)
class BoundLimits:
    max_payload_bytes: int = 8 * 1024 * 1024
    max_parent_depth: int = MAX_PARENT_DEPTH
    max_reference_count: int = 64
    max_pending_captures: int = 2_000
    max_store_bytes: int = 512 * 1024 * 1024
    max_lock_wait_ms: int = 2_000
    max_candles: int = 5_000
    max_events: int = 5_000
    max_progress_rows: int = 128
    max_diagnostics: int = 5_000
    max_delivery_nodes: int = 64


DEFAULT_BOUNDS = BoundLimits()


class BoundExceeded(RuntimeError):
    """A declared capture budget was exhausted."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
