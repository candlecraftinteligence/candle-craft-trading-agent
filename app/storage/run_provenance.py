"""Prospective research provenance for stored scan runs.

Provenance is observational metadata. It must not enter setup fingerprints,
lifecycle identity, public event keys, message hashes, or outcome identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from app.storage.database import SCHEMA_VERSION

PROVENANCE_SCHEMA_VERSION: Final[str] = "cci-run-provenance-v1"
CONFIG_HASH_VERSION: Final[str] = "cci-safe-config-hash-v1"
ALLOWLIST_VERSION: Final[str] = "cci-safe-config-allowlist-v1"
PROVENANCE_RUNTIME_STATS_KEY: Final[str] = "research_provenance"

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")
_REASON_COLLECTION_FAILED: Final[str] = "collection_failed"

SETTINGS_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    ("environment", "identifier"),
    ("local_manual_mode", "bool"),
    ("order_execution_enabled", "bool"),
    ("telegram_dry_run", "bool"),
    ("telegram_signals_enabled", "bool"),
    ("telegram_admin_enabled", "bool"),
    ("telegram_public_watchlist_enabled", "bool"),
    ("telegram_research_watch_enabled", "bool"),
    ("telegram_watchlist_outcome_tracking_enabled", "bool"),
    ("telegram_public_signal_policy", "identifier"),
    ("scanner_confirmation_cycles", "int"),
    ("scanner_setup_merge_tolerance_pct", "number"),
    ("global_context_enabled", "bool"),
    ("btc_context_enabled", "bool"),
    ("btc_d_context_enabled", "bool"),
    ("macro_event_context_enabled", "bool"),
    ("microstructure_flow_enabled", "bool"),
    ("liquidation_flow_enabled", "bool"),
    ("order_book_liquidity_enabled", "bool"),
    ("public_watchlist_min_grade", "identifier"),
    ("public_watchlist_min_score", "int"),
    ("public_watchlist_min_rr", "number"),
    ("public_watchlist_max_per_scan", "int"),
    ("public_watchlist_dedupe_across_modes", "bool"),
    ("public_watchlist_require_plan", "bool"),
    ("public_watchlist_require_entry_zone", "bool"),
    ("public_watchlist_require_invalidation", "bool"),
)

SCANNER_CONFIG_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    ("exchange", "identifier"),
    ("interval", "identifier"),
    ("candle_limit", "int"),
    ("dry_run_alerts", "bool"),
    ("min_score_for_idea", "number"),
    ("min_rr", "number"),
    ("strategy_name", "identifier"),
    ("strategy_modes", "identifier_tuple"),
    ("aggressive_toggle", "bool"),
    ("htf_timeframe", "identifier"),
    ("bias_timeframe", "identifier"),
    ("structure_timeframe", "identifier"),
    ("execution_timeframe", "identifier"),
    ("confirmation_timeframe", "identifier"),
    ("market_regime_enabled", "bool"),
    ("regime_risk_mode", "identifier"),
    ("regime_strictness", "identifier"),
    ("global_context_enabled", "bool"),
    ("btc_context_enabled", "bool"),
    ("btc_d_context_enabled", "bool"),
    ("macro_event_context_enabled", "bool"),
    ("microstructure_flow_enabled", "bool"),
    ("liquidation_flow_enabled", "bool"),
    ("order_book_liquidity_enabled", "bool"),
    ("fast_mode", "bool"),
)

_SECRET_FIELD_FRAGMENTS: Final[tuple[str, ...]] = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "database_url",
    "chat_id",
    "invite",
    "donate",
)


class ProvenanceError(ValueError):
    """Raised when a provenance input cannot be represented safely."""


def unavailable_provenance(reason: str) -> dict[str, Any]:
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "status": "unavailable",
        "reason": _reason_code(reason),
        "run_association": f"scan_runs.runtime_stats_json.{PROVENANCE_RUNTIME_STATS_KEY}",
    }


def collect_run_provenance(
    *,
    scanner_config: Any | None = None,
    settings_values: Mapping[str, Any] | None = None,
    git_info: Mapping[str, Any] | None = None,
    package_version: str | None = None,
    writer_schema_version: int | None = None,
) -> dict[str, Any]:
    """Collect invocation-stable research provenance.

    Secret-bearing objects must not be passed in. Only allowlisted names are
    read from settings/config objects via getattr.
    """

    git_payload = dict(git_info) if git_info is not None else dict(_cached_git_info())
    settings_snapshot, settings_invalid = _settings_snapshot(settings_values)
    config_snapshot, config_invalid = _scanner_config_snapshot(scanner_config)
    canonical = {
        "allowlist_version": ALLOWLIST_VERSION,
        "scanner_config": config_snapshot,
        "settings": settings_snapshot,
    }
    excluded = tuple(dict.fromkeys((*settings_invalid, *config_invalid)))
    config_hash = _hash_canonical(canonical)
    commit_sha = git_payload.get("commit_sha")
    dirty = git_payload.get("dirty_working_tree")
    git_status = git_payload.get("status", "unavailable")
    package = _validated_identifier(package_version) if package_version is not None else _package_version()
    schema_version = SCHEMA_VERSION if writer_schema_version is None else int(writer_schema_version)
    strategy_name = config_snapshot.get("strategy_name")
    reproducible = bool(
        git_status == "captured"
        and isinstance(commit_sha, str)
        and dirty is False
    )
    overall = "captured"
    if git_status != "captured" or excluded:
        overall = "partial" if config_hash else "unavailable"
    if not config_hash:
        return unavailable_provenance("safe_configuration_hash_unavailable")
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "status": overall,
        "run_association": f"scan_runs.runtime_stats_json.{PROVENANCE_RUNTIME_STATS_KEY}",
        "git": {
            "status": git_status,
            "commit_sha": commit_sha if isinstance(commit_sha, str) else None,
            "dirty_working_tree": dirty if isinstance(dirty, bool) else None,
            "reproducible_from_commit_alone": reproducible,
        },
        "versions": {
            "package_version": package,
            "strategy_version": {"status": "unavailable"},
            "strategy_name": strategy_name,
            "configuration_version": {"status": "unavailable"},
            "writer_schema_version": schema_version,
        },
        "safe_configuration": {
            "hash_version": CONFIG_HASH_VERSION,
            "hash_sha256": config_hash,
            "allowlist_version": ALLOWLIST_VERSION,
            "canonical_values": canonical,
            "excluded_invalid_fields": list(excluded),
        },
    }


def safe_configuration_hash(
    *,
    scanner_config: Any | None = None,
    settings_values: Mapping[str, Any] | None = None,
) -> str:
    settings_snapshot, _ = _settings_snapshot(settings_values)
    config_snapshot, _ = _scanner_config_snapshot(scanner_config)
    return _hash_canonical(
        {
            "allowlist_version": ALLOWLIST_VERSION,
            "scanner_config": config_snapshot,
            "settings": settings_snapshot,
        }
    )


def _reason_code(reason: str) -> str:
    text = str(reason).strip().lower().replace(" ", "_")
    if not text or not re.fullmatch(r"[a-z0-9_]{1,64}", text):
        return _REASON_COLLECTION_FAILED
    return text


def _package_version() -> str | dict[str, str]:
    try:
        from app import __version__

        identifier = _validated_identifier(__version__)
        if identifier is None:
            return {"status": "unavailable"}
        return identifier
    except Exception:
        return {"status": "unavailable"}


@lru_cache(maxsize=1)
def _cached_git_info() -> Mapping[str, Any]:
    return _detect_git_info()


def reset_git_info_cache() -> None:
    _cached_git_info.cache_clear()


def _detect_git_info() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    try:
        inside = _git_output(["rev-parse", "--is-inside-work-tree"], cwd=root)
        if inside != "true":
            return {"status": "unavailable"}
        sha = _git_output(["rev-parse", "HEAD"], cwd=root)
        if not _COMMIT_SHA_RE.fullmatch(sha):
            return {"status": "unavailable"}
        porcelain = _git_output(["status", "--porcelain"], cwd=root, allow_empty=True)
        return {
            "status": "captured",
            "commit_sha": sha,
            "dirty_working_tree": bool(porcelain),
        }
    except Exception:
        return {"status": "unavailable"}


def _git_output(args: list[str], *, cwd: Path, allow_empty: bool = False) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise ProvenanceError("git_unavailable")
    output = completed.stdout.strip()
    if not output and not allow_empty:
        raise ProvenanceError("git_unavailable")
    return output


def _settings_snapshot(
    settings_values: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    source: Any
    if settings_values is not None:
        source = settings_values
    else:
        try:
            from app.core.config import get_settings

            source = get_settings()
        except Exception:
            return {}, ("settings",)
    return _extract_allowlist(source, SETTINGS_ALLOWLIST, prefix="settings.")


def _scanner_config_snapshot(scanner_config: Any | None) -> tuple[dict[str, Any], tuple[str, ...]]:
    if scanner_config is None:
        return {}, ("scanner_config",)
    return _extract_allowlist(scanner_config, SCANNER_CONFIG_ALLOWLIST, prefix="scanner_config.")


def _extract_allowlist(
    source: Any,
    allowlist: tuple[tuple[str, str], ...],
    *,
    prefix: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    snapshot: dict[str, Any] = {}
    invalid: list[str] = []
    for name, kind in allowlist:
        if _field_looks_secret(name):
            invalid.append(f"{prefix}{name}")
            continue
        try:
            value = _read_allowlisted_value(source, name)
        except Exception:
            invalid.append(f"{prefix}{name}")
            continue
        coerced = _coerce_allowlisted_value(value, kind)
        if coerced is _INVALID:
            invalid.append(f"{prefix}{name}")
            continue
        snapshot[name] = coerced
    return snapshot, tuple(invalid)


def _read_allowlisted_value(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        if name not in source:
            raise KeyError(name)
        return source[name]
    if not hasattr(source, name):
        raise AttributeError(name)
    return getattr(source, name)


def _field_looks_secret(name: str) -> bool:
    lowered = name.casefold()
    return any(fragment in lowered for fragment in _SECRET_FIELD_FRAGMENTS)


_INVALID = object()


def _coerce_allowlisted_value(value: Any, kind: str) -> Any:
    if kind == "bool":
        return value if type(value) is bool else _INVALID
    if kind == "int":
        if type(value) is bool or type(value) is not int:
            return _INVALID
        if value < 0 or value > 1_000_000:
            return _INVALID
        return value
    if kind == "number":
        return _canonical_number(value)
    if kind == "identifier":
        return _validated_identifier(value) if _validated_identifier(value) is not None else _INVALID
    if kind == "identifier_tuple":
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            return _INVALID
        items: list[str] = []
        for item in value:
            identifier = _validated_identifier(_enum_or_text(item))
            if identifier is None:
                return _INVALID
            items.append(identifier)
        return tuple(sorted(items))
    return _INVALID


def _enum_or_text(value: Any) -> Any:
    raw = getattr(value, "value", value)
    return raw


def _canonical_number(value: Any) -> Any:
    if type(value) is bool:
        return _INVALID
    if type(value) is int:
        return value
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return _INVALID
    if not decimal_value.is_finite():
        return _INVALID
    if isinstance(value, float) and not math.isfinite(value):
        return _INVALID
    normalized = format(decimal_value.normalize(), "f")
    if normalized == "-0":
        normalized = "0"
    return normalized


def _validated_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.casefold()
    if "://" in text or "\\" in text or any(part in lowered for part in ("token=", "password", "secret")):
        return None
    if not _IDENTIFIER_RE.fullmatch(text):
        return None
    return text


def _hash_canonical(payload: Mapping[str, Any]) -> str:
    encoded = canonical_json_dumps(payload)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_json_dumps(payload: Any) -> str:
    return json.dumps(
        _canonicalize(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _canonicalize(value: Any) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if isinstance(value, Decimal):
        return _canonical_number(value)
    if isinstance(value, tuple) or isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in value}
    raise ProvenanceError("unsupported_canonical_type")


def collect_run_provenance_safe(**kwargs: Any) -> dict[str, Any]:
    try:
        return collect_run_provenance(**kwargs)
    except Exception:
        return unavailable_provenance(_REASON_COLLECTION_FAILED)


__all__ = [
    "ALLOWLIST_VERSION",
    "CONFIG_HASH_VERSION",
    "PROVENANCE_RUNTIME_STATS_KEY",
    "PROVENANCE_SCHEMA_VERSION",
    "SCANNER_CONFIG_ALLOWLIST",
    "SETTINGS_ALLOWLIST",
    "canonical_json_dumps",
    "collect_run_provenance",
    "collect_run_provenance_safe",
    "reset_git_info_cache",
    "safe_configuration_hash",
    "unavailable_provenance",
]
