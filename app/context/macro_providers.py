from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.context.macro_events import (
    EVENT_CATEGORY_BY_TYPE,
    RESEARCH_PRIORITY_BY_TYPE,
    MacroEvent,
    MacroEventType,
    MacroSourceStatus,
    MacroVerificationStatus,
    US_EASTERN_TIMEZONE,
    build_event_id,
    normalize_event_type,
    strict_aware_utc,
)
from app.data.exceptions import (
    ExchangeHTTPError,
    ExchangeMalformedJSONError,
    ExchangeNetworkError,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeTimeoutError,
)
from app.data.retry import retry_async


BLS_SOURCE: Final = "bls"
FED_SOURCE: Final = "federal_reserve"
BEA_SOURCE: Final = "bea"
BLS_CALENDAR_URL: Final = "https://www.bls.gov/schedule/news_release/bls.ics"
FED_CALENDAR_URL: Final = "https://www.federalreserve.gov/newsevents/calendar.htm"
FED_MONTH_URL_TEMPLATE: Final = "https://www.federalreserve.gov/newsevents/{year}-{month}.htm"
BEA_RELEASE_DATES_URL: Final = "https://apps.bea.gov/API/signup/release_dates.json"
MACRO_CALENDAR_USER_AGENT: Final = (
    "candle-craft-trading-agent/macro-event-research "
    "(+https://github.com/candlecraftinteligence/candle-craft-trading-agent)"
)
DEFAULT_MACRO_REQUEST_TIMEOUT_SECONDS: Final = 8.0
DEFAULT_MACRO_RETRY_ATTEMPTS: Final = 2
DEFAULT_MACRO_RETRY_BASE_DELAY_SECONDS: Final = 0.25
DEFAULT_MACRO_RETRY_MAX_DELAY_SECONDS: Final = 1.0
MAX_CALENDAR_RESPONSE_BYTES: Final = 2_000_000
_SCOPED_EVENT_TYPES: Final = frozenset(
    event_type
    for event_type in MacroEventType
    if event_type != MacroEventType.OTHER_OFFICIAL_MACRO_EVENT
)

logger = logging.getLogger(__name__)


class MacroProviderObservation(BaseModel):
    source: str
    source_url: str
    fetched_at: datetime
    events: tuple[MacroEvent, ...]
    status: MacroSourceStatus = MacroSourceStatus.VERIFIED
    reason: str | None = None
    calendar_version: str | None = None
    request_count: int = Field(default=0, ge=0)

    model_config = ConfigDict(frozen=True)

    @field_validator("fetched_at", mode="before")
    @classmethod
    def _fetched_at_aware(cls, value: Any) -> datetime:
        return strict_aware_utc(value, field_name="provider_fetched_at")


class MacroCalendarProvider(Protocol):
    source: str
    source_url: str

    async def fetch(self) -> MacroProviderObservation:
        ...


class _OfficialHttpProvider:
    source: str
    source_url: str

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_MACRO_REQUEST_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_MACRO_RETRY_ATTEMPTS,
        retry_base_delay_seconds: float = DEFAULT_MACRO_RETRY_BASE_DELAY_SECONDS,
        retry_max_delay_seconds: float = DEFAULT_MACRO_RETRY_MAX_DELAY_SECONDS,
        clock: Callable[[], datetime] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("macro calendar request timeout must be greater than zero")
        if retry_attempts < 1:
            raise ValueError("macro calendar retry attempts must be at least one")
        self._http_client = http_client
        self._timeout_seconds = float(timeout_seconds)
        self._retry_attempts = int(retry_attempts)
        self._retry_base_delay_seconds = max(float(retry_base_delay_seconds), 0.0)
        self._retry_max_delay_seconds = max(float(retry_max_delay_seconds), 0.0)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._logger = log or logger
        self.request_count = 0

    async def _get_text(self, url: str, *, accept: str) -> str:
        async def request_once() -> str:
            self.request_count += 1
            owns_client = self._http_client is None
            client = self._http_client or httpx.AsyncClient(
                timeout=self._timeout_seconds,
                headers={"User-Agent": MACRO_CALENDAR_USER_AGENT, "Accept": accept},
                follow_redirects=True,
            )
            try:
                try:
                    response = await client.get(
                        url,
                        headers={"User-Agent": MACRO_CALENDAR_USER_AGENT, "Accept": accept},
                        timeout=self._timeout_seconds,
                        follow_redirects=True,
                    )
                except httpx.TimeoutException as exc:
                    raise ExchangeTimeoutError(
                        f"{self.source} calendar request timed out"
                    ) from exc
                except httpx.TransportError as exc:
                    raise ExchangeNetworkError(
                        f"{self.source} calendar request failed"
                    ) from exc
            finally:
                if owns_client:
                    await client.aclose()
            if response.status_code == 429:
                raise ExchangeRateLimitError(
                    f"{self.source} calendar rate limit response: HTTP 429",
                    status_code=429,
                    retry_after=_retry_after(response),
                )
            if not 200 <= response.status_code < 300:
                raise ExchangeHTTPError(
                    f"{self.source} calendar non-200 response: HTTP {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text[:500],
                )
            if len(response.content) > MAX_CALENDAR_RESPONSE_BYTES:
                raise ExchangeResponseError(
                    f"{self.source} calendar response exceeds size limit"
                )
            return response.text

        return await retry_async(
            request_once,
            attempts=self._retry_attempts,
            base_delay=self._retry_base_delay_seconds,
            max_delay=self._retry_max_delay_seconds,
            logger=self._logger,
            operation_name=f"{self.source} official calendar GET",
        )

    def _fetched_at(self) -> datetime:
        return strict_aware_utc(self._clock(), field_name=f"{self.source}_fetch_clock")


class BlsCalendarProvider(_OfficialHttpProvider):
    source = BLS_SOURCE
    source_url = BLS_CALENDAR_URL

    async def fetch(self) -> MacroProviderObservation:
        before_requests = self.request_count
        payload = await self._get_text(
            self.source_url,
            accept="text/calendar,text/plain;q=0.9,*/*;q=0.1",
        )
        parsed_events, warnings = parse_bls_ics(payload)
        scoped = tuple(
            event
            for event in parsed_events
            if event.normalized_event_type in _SCOPED_EVENT_TYPES
        )
        if not scoped and warnings:
            raise ExchangeResponseError(
                f"BLS calendar contained no usable scoped events: {'; '.join(warnings)}"
            )
        return MacroProviderObservation(
            source=self.source,
            source_url=self.source_url,
            fetched_at=self._fetched_at(),
            events=scoped,
            status=MacroSourceStatus.PARTIAL if warnings else MacroSourceStatus.VERIFIED,
            reason="; ".join(warnings) if warnings else None,
            calendar_version=_calendar_version(scoped),
            request_count=self.request_count - before_requests,
        )


class BeaCalendarProvider(_OfficialHttpProvider):
    source = BEA_SOURCE
    source_url = BEA_RELEASE_DATES_URL

    async def fetch(self) -> MacroProviderObservation:
        before_requests = self.request_count
        payload_text = await self._get_text(
            self.source_url,
            accept="application/json,*/*;q=0.1",
        )
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ExchangeMalformedJSONError(
                "BEA release-dates response is malformed JSON"
            ) from exc
        events, warnings, source_version = parse_bea_release_dates(payload)
        if not events:
            raise ExchangeResponseError("BEA release-dates response has no scoped events")
        return MacroProviderObservation(
            source=self.source,
            source_url=self.source_url,
            fetched_at=self._fetched_at(),
            events=events,
            status=MacroSourceStatus.PARTIAL if warnings else MacroSourceStatus.VERIFIED,
            reason="; ".join(warnings) if warnings else None,
            calendar_version=source_version or _calendar_version(events),
            request_count=self.request_count - before_requests,
        )


class FederalReserveCalendarProvider(_OfficialHttpProvider):
    source = FED_SOURCE
    source_url = FED_CALENDAR_URL

    async def fetch(self) -> MacroProviderObservation:
        before_requests = self.request_count
        now = self._fetched_at()
        months = _adjacent_months(now)
        urls = tuple(
            FED_MONTH_URL_TEMPLATE.format(year=year, month=month_name.lower())
            for year, month_name in months
        )
        responses = await asyncio.gather(
            *(
                self._get_text(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.1")
                for url in urls
            ),
            return_exceptions=True,
        )
        events: list[MacroEvent] = []
        warnings: list[str] = []
        page_versions: list[str] = []
        successful_pages = 0
        for (year, month_name), url, response in zip(months, urls, responses, strict=True):
            if isinstance(response, BaseException):
                warnings.append(
                    f"{year}-{month_name.lower()} unavailable: {_clean_reason(response)}"
                )
                continue
            successful_pages += 1
            page_events, page_warnings = parse_federal_reserve_month_html(
                response,
                year=year,
                month_name=month_name,
                source_url=url,
            )
            events.extend(page_events)
            warnings.extend(page_warnings)
            page_versions.append(sha256(response.encode("utf-8")).hexdigest()[:16])
        if successful_pages == 0:
            raise ExchangeResponseError(
                "Federal Reserve monthly calendars are unavailable: " + "; ".join(warnings)
            )
        scoped = tuple(
            event
            for event in events
            if event.normalized_event_type in _SCOPED_EVENT_TYPES
        )
        return MacroProviderObservation(
            source=self.source,
            source_url=self.source_url,
            fetched_at=now,
            events=scoped,
            status=(
                MacroSourceStatus.VERIFIED
                if successful_pages == len(urls) and not warnings
                else MacroSourceStatus.PARTIAL
            ),
            reason="; ".join(warnings) if warnings else None,
            calendar_version=(
                f"pages:{'-'.join(page_versions)}" if page_versions else _calendar_version(scoped)
            ),
            request_count=self.request_count - before_requests,
        )


def parse_bls_ics(payload: str) -> tuple[tuple[MacroEvent, ...], tuple[str, ...]]:
    raw_events = _parse_ics_components(payload)
    events: list[MacroEvent] = []
    warnings: list[str] = []
    for index, properties in enumerate(raw_events, start=1):
        try:
            summary = _ics_property_value(properties, "SUMMARY")
            start_value, start_params = _ics_property(properties, "DTSTART")
            scheduled_at = _parse_ics_datetime(
                start_value,
                start_params,
                default_timezone=US_EASTERN_TIMEZONE,
            )
            event_type = normalize_event_type(BLS_SOURCE, summary)
            source_event_id = _optional_ics_property_value(properties, "UID")
            sequence_text = _optional_ics_property_value(properties, "SEQUENCE")
            sequence = int(sequence_text) if sequence_text not in (None, "") else None
            events.append(
                MacroEvent(
                    event_id=build_event_id(
                        source=BLS_SOURCE,
                        source_event_id=source_event_id,
                        normalized_event_type=event_type,
                        source_event_name=summary,
                        scheduled_at_utc=scheduled_at,
                    ),
                    source=BLS_SOURCE,
                    source_event_id=source_event_id,
                    source_event_name=summary,
                    normalized_event_type=event_type,
                    scheduled_at_utc=scheduled_at,
                    source_timezone=US_EASTERN_TIMEZONE,
                    event_category=EVENT_CATEGORY_BY_TYPE[event_type],
                    institution="U.S. Bureau of Labor Statistics",
                    research_priority=RESEARCH_PRIORITY_BY_TYPE[event_type],
                    verification_status=MacroVerificationStatus.VERIFIED,
                    calendar_sequence=sequence,
                )
            )
        except Exception as exc:
            warnings.append(f"BLS VEVENT {index} ignored: {_clean_reason(exc)}")
    return tuple(events), tuple(warnings)


def parse_bea_release_dates(
    payload: Any,
) -> tuple[tuple[MacroEvent, ...], tuple[str, ...], str | None]:
    if not isinstance(payload, Mapping):
        raise ExchangeResponseError("BEA release-dates response must be an object")
    events: list[MacroEvent] = []
    warnings: list[str] = []
    for source_name in ("Gross Domestic Product", "Personal Income and Outlays"):
        event_type = normalize_event_type(BEA_SOURCE, source_name)
        item = payload.get(source_name)
        if not isinstance(item, Mapping) or not isinstance(item.get("release_dates"), Sequence):
            warnings.append(f"BEA {source_name} release_dates missing")
            continue
        for index, raw_time in enumerate(item["release_dates"]):
            try:
                scheduled_at = strict_aware_utc(
                    raw_time,
                    field_name=f"BEA {source_name} release date",
                )
                source_event_id = f"{source_name}|{scheduled_at.isoformat()}"
                events.append(
                    MacroEvent(
                        event_id=build_event_id(
                            source=BEA_SOURCE,
                            source_event_id=source_event_id,
                            normalized_event_type=event_type,
                            source_event_name=source_name,
                            scheduled_at_utc=scheduled_at,
                        ),
                        source=BEA_SOURCE,
                        source_event_id=source_event_id,
                        source_event_name=source_name,
                        normalized_event_type=event_type,
                        scheduled_at_utc=scheduled_at,
                        source_timezone="UTC",
                        event_category=EVENT_CATEGORY_BY_TYPE[event_type],
                        institution="U.S. Bureau of Economic Analysis",
                        research_priority=RESEARCH_PRIORITY_BY_TYPE[event_type],
                    )
                )
            except Exception as exc:
                warnings.append(
                    f"BEA {source_name} release date {index} ignored: {_clean_reason(exc)}"
                )
    version_value = payload.get("file_last_updated")
    source_version = (
        f"file_last_updated:{str(version_value).strip()}"
        if version_value not in (None, "")
        else None
    )
    return tuple(events), tuple(warnings), source_version


def parse_federal_reserve_month_html(
    payload: str,
    *,
    year: int,
    month_name: str,
    source_url: str,
) -> tuple[tuple[MacroEvent, ...], tuple[str, ...]]:
    del source_url  # Source URL is retained once in source health, not on every event.
    parser = _TreeHtmlParser()
    parser.feed(payload)
    parser.close()
    events: list[MacroEvent] = []
    warnings: list[str] = []
    category: str | None = None
    for node in _walk_nodes(parser.root):
        if node.tag != "div":
            continue
        classes = node.class_names
        if "cal-nojs__rowTitle" in classes:
            heading = next((child for child in _walk_nodes(node) if child.tag == "h4"), None)
            category = _node_text(heading) if heading is not None else None
            continue
        if "row" not in classes or "cal-nojs__rowTitle" in classes:
            continue
        columns = {
            class_name: child
            for child in node.children
            if child.tag == "div"
            for class_name in child.class_names
            if class_name in {"col-xs-2", "col-xs-7", "col-xs-3"}
        }
        if not {"col-xs-2", "col-xs-7", "col-xs-3"}.issubset(columns):
            continue
        if category not in {"FOMC Meetings", "Speeches"}:
            continue
        time_text = _node_text(columns["col-xs-2"])
        date_text = _node_text(columns["col-xs-3"])
        detail_paragraphs = tuple(
            _node_text(item)
            for item in _walk_nodes(columns["col-xs-7"])
            if item.tag == "p" and _node_text(item)
        )
        if not detail_paragraphs:
            continue
        source_event_name = detail_paragraphs[0]
        event_type = normalize_event_type(FED_SOURCE, source_event_name)
        if event_type == MacroEventType.OTHER_OFFICIAL_MACRO_EVENT:
            continue
        try:
            event_time = datetime.strptime(
                _normalize_meridiem(time_text), "%I:%M %p"
            ).time()
            days = tuple(int(value) for value in re.findall(r"\b\d{1,2}\b", date_text))
            if not days:
                raise ValueError("release day is missing")
            speaker = _fed_speaker(source_event_name)
            title = _fed_title(columns["col-xs-7"], detail_paragraphs)
            for day in days:
                scheduled_local = datetime.combine(
                    datetime.strptime(f"{year}-{month_name}-{day}", "%Y-%B-%d").date(),
                    event_time,
                    tzinfo=ZoneInfo(US_EASTERN_TIMEZONE),
                )
                scheduled_at = scheduled_local.astimezone(UTC)
                source_event_id = "|".join(
                    (
                        f"{year}-{month_name}-{day}",
                        time_text,
                        source_event_name,
                        speaker or "",
                    )
                )
                events.append(
                    MacroEvent(
                        event_id=build_event_id(
                            source=FED_SOURCE,
                            source_event_id=source_event_id,
                            normalized_event_type=event_type,
                            source_event_name=source_event_name,
                            scheduled_at_utc=scheduled_at,
                        ),
                        source=FED_SOURCE,
                        source_event_id=source_event_id,
                        source_event_name=source_event_name,
                        normalized_event_type=event_type,
                        scheduled_at_utc=scheduled_at,
                        source_timezone=US_EASTERN_TIMEZONE,
                        event_category=EVENT_CATEGORY_BY_TYPE[event_type],
                        speaker=speaker,
                        title=title,
                        institution="Board of Governors of the Federal Reserve System",
                        research_priority=RESEARCH_PRIORITY_BY_TYPE[event_type],
                    )
                )
        except Exception as exc:
            warnings.append(
                f"Fed {month_name} {source_event_name} ignored: {_clean_reason(exc)}"
            )
    return tuple(events), tuple(warnings)


def _parse_ics_components(
    payload: str,
) -> tuple[dict[str, list[tuple[str, Mapping[str, str]]]], ...]:
    unfolded: list[str] = []
    for line in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    components: list[dict[str, list[tuple[str, Mapping[str, str]]]]] = []
    current: dict[str, list[tuple[str, Mapping[str, str]]]] | None = None
    for line in unfolded:
        if line.strip().upper() == "BEGIN:VEVENT":
            current = {}
            continue
        if line.strip().upper() == "END:VEVENT":
            if current is not None:
                components.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        raw_key, value = line.split(":", 1)
        key_parts = raw_key.split(";")
        name = key_parts[0].strip().upper()
        params: dict[str, str] = {}
        for raw_param in key_parts[1:]:
            if "=" in raw_param:
                param_name, param_value = raw_param.split("=", 1)
                params[param_name.strip().upper()] = param_value.strip().strip('"')
        current.setdefault(name, []).append((_unescape_ics(value), params))
    if not components and "BEGIN:VCALENDAR" not in payload.upper():
        raise ExchangeResponseError("calendar payload is not ICS")
    return tuple(components)


def _ics_property(
    properties: Mapping[str, Sequence[tuple[str, Mapping[str, str]]]], name: str
) -> tuple[str, Mapping[str, str]]:
    values = properties.get(name)
    if not values:
        raise ValueError(f"ICS {name} is missing")
    return values[0]


def _ics_property_value(
    properties: Mapping[str, Sequence[tuple[str, Mapping[str, str]]]], name: str
) -> str:
    return _ics_property(properties, name)[0]


def _optional_ics_property_value(
    properties: Mapping[str, Sequence[tuple[str, Mapping[str, str]]]], name: str
) -> str | None:
    values = properties.get(name)
    return values[0][0] if values else None


def _parse_ics_datetime(
    value: str,
    params: Mapping[str, str],
    *,
    default_timezone: str | None,
) -> datetime:
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        raise ValueError("date-only calendar event has no verified release time")
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    timezone_name = params.get("TZID") or default_timezone
    if not timezone_name:
        raise ValueError("floating calendar time has no source timezone")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown calendar timezone: {timezone_name}") from exc
    parsed: datetime | None = None
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            parsed = datetime.strptime(value, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("calendar time is malformed")
    return parsed.replace(tzinfo=timezone).astimezone(UTC)


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


@dataclass
class _HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_HtmlNode] = field(default_factory=list)
    data: list[str] = field(default_factory=list)

    @property
    def class_names(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())


class _TreeHtmlParser(HTMLParser):
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(tag=tag, attrs={key: value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].data.append(data)


def _walk_nodes(node: _HtmlNode) -> Sequence[_HtmlNode]:
    result: list[_HtmlNode] = [node]
    for child in node.children:
        result.extend(_walk_nodes(child))
    return tuple(result)


def _node_text(node: _HtmlNode | None) -> str:
    if node is None:
        return ""
    values = list(node.data)
    for child in node.children:
        child_text = _node_text(child)
        if child_text:
            values.append(child_text)
    return " ".join(" ".join(values).split())


def _fed_speaker(source_event_name: str) -> str | None:
    for prefix in ("Speech - ", "Discussion - "):
        if source_event_name.startswith(prefix):
            return source_event_name[len(prefix) :].strip() or None
    return None


def _fed_title(column: _HtmlNode, paragraphs: Sequence[str]) -> str | None:
    title_node = next(
        (
            node
            for node in _walk_nodes(column)
            if node.tag == "p" and "calendar__title" in node.class_names
        ),
        None,
    )
    if title_node is not None:
        title = _node_text(title_node)
        return title or None
    return paragraphs[1] if len(paragraphs) > 1 and paragraphs[1] != "Watch Live" else None


def _normalize_meridiem(value: str) -> str:
    normalized = " ".join(value.replace(".", "").split()).upper()
    return normalized


def _adjacent_months(now: datetime) -> tuple[tuple[int, str], ...]:
    month_names = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )
    result: list[tuple[int, str]] = []
    for offset in (-1, 0, 1):
        month_index = now.month - 1 + offset
        year = now.year + month_index // 12
        normalized_index = month_index % 12
        result.append((year, month_names[normalized_index]))
    return tuple(result)


def _calendar_version(events: Sequence[MacroEvent]) -> str:
    identity = "\n".join(
        "|".join(
            (
                event.event_id,
                event.normalized_event_type.value,
                event.scheduled_at_utc.isoformat(),
                event.source_event_name,
                str(event.calendar_sequence or 0),
            )
        )
        for event in sorted(events, key=lambda item: (item.scheduled_at_utc, item.event_id))
    )
    return f"normalized-sha256:{sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return None


def _clean_reason(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


__all__ = [
    "BEA_RELEASE_DATES_URL",
    "BEA_SOURCE",
    "BLS_CALENDAR_URL",
    "BLS_SOURCE",
    "FED_CALENDAR_URL",
    "FED_MONTH_URL_TEMPLATE",
    "FED_SOURCE",
    "BeaCalendarProvider",
    "BlsCalendarProvider",
    "FederalReserveCalendarProvider",
    "MacroCalendarProvider",
    "MacroProviderObservation",
    "parse_bea_release_dates",
    "parse_bls_ics",
    "parse_federal_reserve_month_html",
]
