"""Optional, context-local observation of repository completion; no SQL authority."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TransactionObservation:
    outcome: str = "NOT_OBSERVED"
    connection_closed: bool | None = None


_current: ContextVar[TransactionObservation | None] = ContextVar(
    "cci_lifecycle_transaction_observation", default=None,
)


@contextmanager
def observe_transaction():
    observation = TransactionObservation()
    token = _current.set(observation)
    try:
        yield observation
    finally:
        _current.reset(token)


def note_repository_completion(*, committed: bool, commit_failed: bool,
                               rolled_back: bool, closed: bool) -> None:
    observation = _current.get()
    if observation is None:
        return
    observation.connection_closed = closed
    observation.outcome = (
        "CLOSE_FAILED" if not closed else
        "COMMIT_FAILED_ROLLED_BACK" if commit_failed and rolled_back else
        "COMMIT_FAILED_UNKNOWN" if commit_failed else
        "COMMITTED" if committed else
        "ROLLED_BACK" if rolled_back else "NO_TRANSACTION_OR_UNKNOWN"
    )
