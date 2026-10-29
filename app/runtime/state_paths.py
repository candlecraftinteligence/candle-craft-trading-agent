"""Explicit runtime storage routing; resolution never creates or migrates state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class RuntimeStatePaths:
    root: Path

    @classmethod
    def from_root(
        cls, root: Path | str, *, legacy_roots: Sequence[Path | str],
    ) -> RuntimeStatePaths:
        resolved = Path(root).resolve()
        if resolved.exists() and not resolved.is_dir():
            raise ValueError("Runtime state root must be a directory.")
        for legacy in legacy_roots:
            legacy = Path(legacy).resolve()
            if resolved.is_relative_to(legacy) or legacy.is_relative_to(resolved):
                raise ValueError("Runtime state root must be separate from legacy scan_runs storage.")
        return cls(resolved)

    def resolve(self, path: Path | str) -> Path:
        """Relative arguments belong to this root; reject escapes and link aliases."""
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if resolved == self.root or not resolved.is_relative_to(self.root):
            raise ValueError(f"Runtime state path escapes configured root: {path}")
        return resolved


def rebase_state_arguments(
    args: Any,
    paths: RuntimeStatePaths,
    *,
    defaults: Mapping[str, tuple[Path, str]],
    optional: Sequence[str] = (),
) -> None:
    """Map existing argparse defaults and validate every explicit state path first."""
    updates = {}
    for name, (old_default, filename) in defaults.items():
        value = getattr(args, name, None)
        if value is not None:
            updates[name] = paths.resolve(filename if Path(value) == old_default else value)
    for name in optional:
        value = getattr(args, name, None)
        if value is not None:
            updates[name] = paths.resolve(value)
    for name, value in updates.items():
        setattr(args, name, value)
