"""Executing build identity and evaluator implementation fingerprint.

The Runtime epoch's reviewed release SHA is a different claim and stays on the
epoch row. A dirty or unknown git status does not by itself prove the
evaluator source matches.

Attestation v2 covers the semantic modules the closed-candle evaluator and its
repository writes actually execute. Unsupported older attestations fail closed.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Final

import pydantic

from app.research.durable_source_replay.constants import IMPLEMENTATION_ATTESTATION_VERSION

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

IMPLEMENTATION_FILES: Final[tuple[str, ...]] = (
    "app/lifecycle/outcomes.py",
    "app/lifecycle/outcome_policy.py",
    "app/lifecycle/outcome_events.py",
    "app/lifecycle/prefix_disposition_evidence.py",
    "app/lifecycle/plan_version_binding.py",
    "app/lifecycle/economic_identity.py",
    "app/lifecycle/repositories.py",
    "app/lifecycle/state_machine.py",
    "app/lifecycle/models.py",
    "app/core/trade_plan_integrity.py",
    "app/data/candle_integrity.py",
    "app/runtime_epoch/ownership.py",
    "app/runtime_epoch/authority.py",
    "app/runtime_epoch/origin.py",
    "app/runtime_epoch/time_contract.py",
    "app/research/evaluation_policy.py",
    # Capture/replay reconstruction contract is part of exact reproduction.
    "app/research/durable_source_replay/codec.py",
    "app/research/durable_source_replay/prestate.py",
    "app/research/durable_source_replay/delivery.py",
    "app/research/durable_source_replay/constants.py",
)


def dependency_versions() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "pydantic": pydantic.VERSION,
        "implementation_attestation_version": IMPLEMENTATION_ATTESTATION_VERSION,
    }


def implementation_fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(IMPLEMENTATION_ATTESTATION_VERSION.encode("utf-8"))
    digest.update(b"\0")
    for relative in IMPLEMENTATION_FILES:
        path = REPO_ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_identity() -> dict[str, str]:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": "unknown", "dirty": "unknown"}
    text = sha.decode("utf-8", errors="replace").strip()
    if not text:
        return {"git_sha": "unknown", "dirty": "unknown"}
    return {
        "git_sha": text,
        "dirty": "dirty" if dirty.strip() else "clean",
    }
