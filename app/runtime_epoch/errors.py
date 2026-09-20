"""Fatal operational epoch-boundary errors. Never recoverable."""

from __future__ import annotations


class RuntimeEpochError(RuntimeError):
    """Fatal operational epoch/origin/ownership boundary failure."""


class RuntimeEpochConfigurationError(RuntimeEpochError):
    """Missing, mismatched, or unsupported epoch or database selection."""


class RuntimeEpochOwnershipError(RuntimeEpochError):
    """Legacy or mismatched ownership may not be mutated or publicly effected."""


class RuntimeEpochOriginError(RuntimeEpochError):
    """A new operational membership lacks a validated post-boundary origin."""


class RuntimeEpochIdentityCollisionError(RuntimeEpochError):
    """A reobserved legacy lifecycle primary key cannot be reused or relabeled."""
