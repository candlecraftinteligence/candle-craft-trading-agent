"""Shared reconnect pacing and bounded health logging; never verifies market data."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Callable
from typing import Any

from app.microstructure.websocket_protocol import failure_facts


class FeedRecovery:
    def __init__(self, feed: str, base: float, maximum: float, *,
                 reset_after: float = 60.0, monotonic: Callable[[], float] | None = None,
                 jitter: Callable[[float], float] | None = None,
                 log: logging.Logger | None = None) -> None:
        if not all(math.isfinite(v) for v in (base, maximum, reset_after)) or not 0 <= base <= maximum or reset_after <= 0:
            raise ValueError("reconnect limits must be finite and ordered; reset must be positive")
        self.feed, self.base, self.maximum, self.reset_after = feed, base, maximum, reset_after
        self.monotonic = monotonic or time.monotonic
        self.jitter = jitter or (lambda bound: random.uniform(0, bound))
        self.logger = log or logging.getLogger(__name__)
        self.accepted_events = 0
        self.suppressed_logs = 0
        self.last_failure: dict[str, Any] | None = None
        self.receive_count = 0
        self.last_receive_at: float | None = None
        self.heartbeat_timeout_count = 0
        self.start_session()

    def start_session(self) -> None:
        self.failures = 0
        self.delay = 0.0
        self.state = "CONNECTING"
        self.connected_at: float | None = None
        self.outage_started: float | None = None
        self._last_report_at = float("-inf")
        self._reported_level = 0
        self._reported_critical: set[str] = set()
        self._handshake_logged = False

    def connected(self) -> None:
        self.connected_at = self.monotonic()
        self.delay = 0.0
        self.state = "CONNECTED_UNVERIFIED"
        if not self._handshake_logged:
            self.logger.info("%s WebSocket connected; awaiting parsed market data.", self.feed)
            self._handshake_logged = True

    def note_receive(self) -> None:
        """A frame arrived. Parsing may still reject it; this is receive health only."""
        self.receive_count += 1
        self.last_receive_at = self.monotonic()

    def market_event(self) -> None:
        self.accepted_events += 1
        now = self.monotonic()
        if self.failures and self.connected_at is not None and now - self.connected_at >= self.reset_after:
            self.logger.info(
                "%s WebSocket recovered after attempts=%s outage_seconds=%.1f suppressed_total=%s; "
                "parsed frames resumed, evidence coverage remains snapshot-specific.",
                self.feed, self.failures, now - (self.outage_started if self.outage_started is not None else now),
                self.suppressed_logs,
            )
            self.failures = 0
            self.outage_started = None
            self._last_report_at = float("-inf")
            self._reported_level = 0
            self._reported_critical.clear()
        self.state = "RECEIVING_UNSTABLE" if self.failures else "RECEIVING"

    def failed(self, exc: Exception, phase: str) -> float:
        now = self.monotonic()
        self.failures += 1
        self.state = "DISCONNECTED"
        self.last_failure = failure_facts(exc, phase)
        if self.last_failure["category"] == "heartbeat_timeout":
            self.heartbeat_timeout_count += 1
        if self.outage_started is None:
            self.outage_started = now
        nominal = min(self.maximum, self.base * 2 ** min(self.failures - 1, 10))
        bound = nominal * 0.2
        sample = self.jitter(bound)
        if not math.isfinite(sample):
            raise ValueError("reconnect jitter must be finite")
        self.delay = nominal - bound + min(max(sample, 0.0), bound)
        critical = self.last_failure["category"] in {
            "subscription_rejected", "tls_certificate_failure", "protocol_failure", "upgrade_rejected", "proxy_failure",
        }
        new_critical = critical and self.last_failure["category"] not in self._reported_critical
        degraded = critical or self.failures >= 3 or now - self.outage_started >= 60
        level = logging.WARNING if degraded else logging.INFO
        if self.failures == 1 or new_critical or level > self._reported_level or now - self._last_report_at >= 60:
            self.logger.log(
                level,
                "%s WebSocket disconnected attempt=%s phase=%s retry_in=%.1fs "
                "category=%s exception=%s http_status=%s received_close=%s sent_close=%s "
                "suppressed_total=%s evidence=UNVERIFIED.",
                self.feed, self.failures, phase, self.delay,
                self.last_failure["category"], self.last_failure["exception"], self.last_failure["http_status"],
                self.last_failure["received_close_code"], self.last_failure["sent_close_code"], self.suppressed_logs,
            )
            self._last_report_at, self._reported_level = now, level
            if critical:
                self._reported_critical.add(self.last_failure["category"])
        else:
            self.suppressed_logs += 1
        return self.delay

    def stopped(self) -> None:
        self.state = "STOPPED"
        self.delay = 0.0

    def health(self) -> dict[str, Any]:
        return {
            "consecutive_connection_failures": self.failures,
            "reconnect_delay_seconds": self.delay,
            "connection_state": self.state,
            "accepted_market_event_count": self.accepted_events,
            "suppressed_disconnect_logs": self.suppressed_logs,
            "last_failure": self.last_failure,
            "receive_count": self.receive_count,
            "last_receive_age_seconds": (
                None if self.last_receive_at is None
                else round(max(0.0, self.monotonic() - self.last_receive_at), 3)
            ),
            "heartbeat_timeout_count": self.heartbeat_timeout_count,
        }


async def close_connection(connection: Any, *, timeout: float = 5.0) -> None:
    """Own cleanup through cancellation; don't let a second start outrun closure."""
    task = asyncio.ensure_future(connection.close())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except Exception:
            _abort(connection)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception:
        _abort(connection)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _abort(connection: Any) -> None:
    transport = getattr(connection, "transport", None)
    if transport is not None:
        transport.abort()
