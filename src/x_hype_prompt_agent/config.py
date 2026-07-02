from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .models import AgentConfig, NewsSourceConfig

DEFAULT_AGENT_CONFIG_PATH = Path("config") / "x_hype_agent.yaml"
DEFAULT_SOURCES_CONFIG_PATH = Path("config") / "x_hype_sources.yaml"
DEFAULT_DATABASE_PATH = Path("scan_runs") / "x_hype_prompt_agent.sqlite"


class ConfigError(RuntimeError):
    """Raised when the X hype prompt agent config cannot be loaded."""


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def load_agent_config(path: Path | str = DEFAULT_AGENT_CONFIG_PATH) -> AgentConfig:
    payload = _load_yaml_mapping(Path(path))
    return AgentConfig(
        min_score_to_send=_int(payload.get("min_score_to_send"), 80),
        breaking_news_score=_int(payload.get("breaking_news_score"), 90),
        max_prompts_per_run=_int(payload.get("max_prompts_per_run"), 2),
        max_prompts_per_day=_int(payload.get("max_prompts_per_day"), 6),
        lookback_hours=_int(payload.get("lookback_hours"), 24),
        freshness_half_life_hours=_int(payload.get("freshness_half_life_hours"), 8),
        duplicate_window_days=_int(payload.get("duplicate_window_days"), 7),
        watch_interval_sec=_int(payload.get("watch_interval_sec"), 3600),
        allow_tier_3_only_items=_bool(payload.get("allow_tier_3_only_items"), False),
        require_source_url=_bool(payload.get("require_source_url"), True),
        telegram_disable_web_page_preview=_bool(payload.get("telegram_disable_web_page_preview"), False),
    )


def load_source_configs(path: Path | str = DEFAULT_SOURCES_CONFIG_PATH) -> tuple[NewsSourceConfig, ...]:
    payload = _load_yaml_mapping(Path(path))
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError("x_hype_sources.yaml must contain a top-level sources list.")

    sources: list[NewsSourceConfig] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        url = str(raw.get("url", "")).strip()
        source_type = str(raw.get("type", "rss")).strip().lower()
        if not name or not url:
            continue
        categories = raw.get("categories", ())
        if isinstance(categories, str):
            parsed_categories = (categories,)
        else:
            parsed_categories = tuple(str(value).strip() for value in categories or () if str(value).strip())
        sources.append(
            NewsSourceConfig(
                name=name,
                type=source_type,
                url=url,
                tier=_int(raw.get("tier"), 2),
                enabled=_bool(raw.get("enabled"), True),
                categories=parsed_categories,
                reliability_weight=_float(raw.get("reliability_weight"), 1.0),
                notes=str(raw.get("notes", "") or ""),
            )
        )
    return tuple(sources)


def configured_database_path(explicit_path: str | None = None) -> Path:
    raw = explicit_path or os.getenv("X_HYPE_AGENT_DB_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_DATABASE_PATH


def configured_log_level(explicit_level: str | None = None) -> str:
    return (explicit_level or os.getenv("X_HYPE_AGENT_LOG_LEVEL") or "INFO").strip().upper()


def telegram_token() -> str | None:
    value = os.getenv("TELEGRAM_X_HYPE_BOT_TOKEN")
    return value.strip() if value and value.strip() else None


def telegram_chat_id(explicit_chat_id: str | None = None) -> str | None:
    value = explicit_chat_id or os.getenv("TELEGRAM_X_HYPE_CHAT_ID")
    return value.strip() if value and value.strip() else None


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        data = _parse_restricted_yaml(text)
    else:
        data = yaml.safe_load(text) or {}

    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a YAML mapping: {path}")
    return data


def _parse_restricted_yaml(text: str) -> dict[str, Any]:
    lines = _clean_yaml_lines(text)
    result: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        indent, stripped = lines[index]
        if indent != 0:
            index += 1
            continue
        key, value = _split_key_value(stripped)
        if value is not None:
            result[key] = _parse_scalar(value)
            index += 1
            continue

        if key == "sources":
            parsed, index = _parse_sources_list(lines, index + 1)
            result[key] = parsed
        else:
            parsed, index = _parse_nested_mapping(lines, index + 1, parent_indent=indent)
            result[key] = parsed
    return result


def _clean_yaml_lines(text: str) -> list[tuple[int, str]]:
    cleaned: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        cleaned.append((indent, line.strip()))
    return cleaned


def _parse_sources_list(lines: list[tuple[int, str]], index: int) -> tuple[list[dict[str, Any]], int]:
    sources: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    while index < len(lines):
        indent, stripped = lines[index]
        if indent == 0:
            break
        if indent == 2 and stripped.startswith("- "):
            if current is not None:
                sources.append(current)
            current = {}
            rest = stripped[2:].strip()
            if rest:
                key, value = _split_key_value(rest)
                current[key] = _parse_scalar(value or "")
            index += 1
            continue
        if current is None:
            index += 1
            continue
        if indent == 4:
            key, value = _split_key_value(stripped)
            if value is None:
                parsed_list, index = _parse_scalar_list(lines, index + 1, parent_indent=indent)
                current[key] = parsed_list
                continue
            current[key] = _parse_scalar(value)
        index += 1
    if current is not None:
        sources.append(current)
    return sources, index


def _parse_nested_mapping(
    lines: list[tuple[int, str]], index: int, *, parent_indent: int
) -> tuple[dict[str, Any], int]:
    nested: dict[str, Any] = {}
    while index < len(lines):
        indent, stripped = lines[index]
        if indent <= parent_indent:
            break
        key, value = _split_key_value(stripped)
        nested[key] = _parse_scalar(value or "")
        index += 1
    return nested, index


def _parse_scalar_list(lines: list[tuple[int, str]], index: int, *, parent_indent: int) -> tuple[list[Any], int]:
    values: list[Any] = []
    while index < len(lines):
        indent, stripped = lines[index]
        if indent <= parent_indent:
            break
        if stripped.startswith("- "):
            values.append(_parse_scalar(stripped[2:].strip()))
        index += 1
    return values, index


def _split_key_value(text: str) -> tuple[str, str | None]:
    if ":" not in text:
        return text.strip(), ""
    key, value = text.split(":", 1)
    value = value.strip()
    return key.strip(), value if value else None


def _parse_scalar(value: str) -> Any:
    text = value.strip()
    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        text = text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
