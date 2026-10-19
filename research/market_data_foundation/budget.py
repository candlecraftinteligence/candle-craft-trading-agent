"""Finite request accounting. A blocked provider does not become a data-quality pass."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from research.market_data_foundation.constants import (
    LOWERED_ONLINE_ROUNDS,
    MAX_CMC_ATTEMPTS,
    MAX_COINGLASS_ATTEMPTS,
    MAX_COINPAPRIKA_ATTEMPTS,
    MAX_CONCURRENCY,
    MAX_HTTP_ATTEMPTS,
    MAX_MCP_DATA_CALLS,
    MAX_RESPONSE_BYTES,
    MAX_RETRIES,
    MAX_TOTAL_EVIDENCE_BYTES,
    ONLINE_DEADLINE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StudyCancelled(RuntimeError):
    def __init__(self, reason: str = "cancelled") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class StudyBudget:
    max_http_attempts: int = MAX_HTTP_ATTEMPTS
    max_cmc_attempts: int = MAX_CMC_ATTEMPTS
    max_coinpaprika_attempts: int = MAX_COINPAPRIKA_ATTEMPTS
    max_coinglass_attempts: int = MAX_COINGLASS_ATTEMPTS
    max_mcp_data_calls: int = MAX_MCP_DATA_CALLS
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_total_bytes: int = MAX_TOTAL_EVIDENCE_BYTES
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    max_retries: int = MAX_RETRIES
    max_concurrency: int = MAX_CONCURRENCY
    online_rounds: int = LOWERED_ONLINE_ROUNDS
    deadline_monotonic: float = field(default_factory=lambda: time.monotonic() + ONLINE_DEADLINE_SECONDS)
    http_attempts: int = 0
    mcp_data_calls: int = 0
    bytes_read: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    stopped_providers: set[str] = field(default_factory=set)
    stop_events: list[dict[str, object]] = field(default_factory=list)
    cancelled: bool = False
    live_sampling_authorized: bool = False
    shared_ip_spare_capacity: str = "unknown"
    study_stopped_reason: str | None = None

    def provider_cap(self, provider: str) -> int:
        caps = {
            "cmc": self.max_cmc_attempts,
            "coinpaprika": self.max_coinpaprika_attempts,
            "coinglass": self.max_coinglass_attempts,
        }
        return caps.get(provider, self.max_http_attempts)

    def ensure_can_attempt(self, provider: str) -> None:
        if self.cancelled:
            raise StudyCancelled()
        if self.study_stopped_reason:
            raise BudgetExceeded(self.study_stopped_reason)
        if time.monotonic() > self.deadline_monotonic:
            raise BudgetExceeded("deadline")
        if provider in self.stopped_providers:
            raise BudgetExceeded(f"{provider}_stopped")
        if self.http_attempts >= self.max_http_attempts:
            raise BudgetExceeded("http_attempts")
        if self.by_provider.get(provider, 0) >= self.provider_cap(provider):
            raise BudgetExceeded(f"{provider}_attempts")
        if self.bytes_read > self.max_total_bytes:
            raise BudgetExceeded("total_bytes")

    def ensure_during_stream(self, pending_bytes: int = 0) -> None:
        """Deadline, cancellation, and remaining bytes apply while a body is arriving."""

        if self.cancelled:
            raise StudyCancelled("cancelled_during_stream")
        if time.monotonic() > self.deadline_monotonic:
            raise BudgetExceeded("deadline_during_stream")
        if self.bytes_read + max(int(pending_bytes), 0) > self.max_total_bytes:
            raise BudgetExceeded("total_bytes_during_stream")

    def record_attempt(self, provider: str, nbytes: int) -> None:
        self.http_attempts += 1
        self.by_provider[provider] = self.by_provider.get(provider, 0) + 1
        self.bytes_read += max(int(nbytes), 0)
        if self.bytes_read > self.max_total_bytes:
            raise BudgetExceeded("total_bytes")

    def record_mcp_data_call(self) -> None:
        if self.mcp_data_calls >= self.max_mcp_data_calls:
            raise BudgetExceeded("mcp_data_calls")
        self.mcp_data_calls += 1

    def stop_provider(self, provider: str, *, reason: str, attempt: int) -> None:
        self.stopped_providers.add(provider)
        self.stop_events.append(
            {"provider": provider, "reason": reason, "attempt": attempt, "scope": "provider"}
        )

    def mark_study_stopped(self, reason: str, *, provider: str, attempt: int) -> None:
        """A study-wide limit stops every later request. The triggering attempt is kept."""

        if self.study_stopped_reason is None:
            self.study_stopped_reason = reason
        self.stop_events.append(
            {"provider": provider, "reason": reason, "attempt": attempt, "scope": "study"}
        )

    def retry_delay_fits(self, delay_seconds: float) -> bool:
        if delay_seconds < 0:
            return False
        return time.monotonic() + delay_seconds <= self.deadline_monotonic

    def snapshot(self) -> dict[str, object]:
        return {
            "http_attempts": self.http_attempts,
            "max_http_attempts": self.max_http_attempts,
            "by_provider": dict(self.by_provider),
            "provider_caps": {
                "cmc": self.max_cmc_attempts,
                "coinpaprika": self.max_coinpaprika_attempts,
                "coinglass": self.max_coinglass_attempts,
            },
            "mcp_data_calls": self.mcp_data_calls,
            "max_mcp_data_calls": self.max_mcp_data_calls,
            "bytes_read": self.bytes_read,
            "max_total_bytes": self.max_total_bytes,
            "max_response_bytes": self.max_response_bytes,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_concurrency": self.max_concurrency,
            "online_rounds": self.online_rounds,
            "stopped_providers": sorted(self.stopped_providers),
            "stop_events": list(self.stop_events),
            "cancelled": self.cancelled,
            "live_sampling_authorized": self.live_sampling_authorized,
            "shared_ip_spare_capacity": self.shared_ip_spare_capacity,
            "study_stopped_reason": self.study_stopped_reason,
        }
