from __future__ import annotations

import random
import time
from collections.abc import Callable


class ReconnectBackoff:
    """Keep flapping sockets backed off until sustained, parsed market traffic."""

    def __init__(
        self, base: float, maximum: float, *,
        monotonic: Callable[[], float] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.base = base
        self.maximum = maximum
        self.monotonic = monotonic or time.monotonic
        self.jitter = jitter or (lambda bound: random.uniform(0, bound))
        self.failures = 0
        self.connected_at = 0.0

    def connected(self) -> None:
        self.connected_at = self.monotonic()

    def market_event(self) -> None:
        # A handshake or subscription ACK alone says nothing about collection.
        if self.monotonic() - self.connected_at >= 60.0:
            self.failures = 0

    def next_delay(self) -> float:
        self.failures += 1
        delay = min(self.maximum, self.base * 2 ** min(self.failures - 1, 10))
        # Keep jitter at the ceiling too, without exceeding the configured cap.
        bound = delay * 0.2
        return delay - bound + min(max(self.jitter(bound), 0.0), bound)
