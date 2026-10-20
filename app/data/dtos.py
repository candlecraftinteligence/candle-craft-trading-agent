from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

NA = "N/A"

MaybeDecimal: TypeAlias = Decimal | Literal["N/A"]
MaybeInt: TypeAlias = int | Literal["N/A"]


class MarketDataDTO(BaseModel):
    exchange: str
    symbol: str
    raw_source: Any | None = None

    model_config = ConfigDict(frozen=True)


class CandleDTO(MarketDataDTO):
    timestamp: int
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_timestamp: MaybeInt = NA
    quote_volume: MaybeDecimal = NA
    trade_count: MaybeInt = NA

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            _reraise_candle_integrity_error(exc)
            raise

    @classmethod
    def model_validate(
        cls,
        obj: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: Any | None = None,
    ) -> CandleDTO:
        try:
            return super().model_validate(
                obj,
                strict=strict,
                from_attributes=from_attributes,
                context=context,
            )
        except ValidationError as exc:
            _reraise_candle_integrity_error(exc)
            raise

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_candle_inputs(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            from app.data.candle_integrity import (
                CandleIntegrityError,
                CandleIntegrityReason,
                normalize_utc_timestamp,
            )

            timeframe = str(data.get("interval") or "unknown").strip().lower() or "unknown"
            raw_ts = data.get("timestamp")
            if isinstance(raw_ts, bool) or not isinstance(raw_ts, int):
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_OPEN_TIMESTAMP,
                    "CandleDTO timestamp must be an integer millisecond value",
                    timeframe=timeframe,
                )
            try:
                normalize_utc_timestamp(raw_ts, field_name="timestamp")
            except ValueError as exc:
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_OPEN_TIMESTAMP,
                    str(exc),
                    timeframe=timeframe,
                ) from exc

            raw_close_ts = data.get("close_timestamp", NA)
            if raw_close_ts != NA and raw_close_ts is not None:
                if isinstance(raw_close_ts, bool) or not isinstance(raw_close_ts, int):
                    raise CandleIntegrityError(
                        CandleIntegrityReason.INVALID_CLOSE_TIMESTAMP,
                        "CandleDTO close_timestamp must be an integer millisecond value or N/A",
                        timeframe=timeframe,
                    )
                try:
                    normalize_utc_timestamp(raw_close_ts, field_name="close_timestamp")
                except ValueError as exc:
                    raise CandleIntegrityError(
                        CandleIntegrityReason.INVALID_CLOSE_TIMESTAMP,
                        str(exc),
                        timeframe=timeframe,
                    ) from exc

            for price_field in ("open", "high", "low", "close"):
                raw_price = data.get(price_field)
                if isinstance(raw_price, bool):
                    raise CandleIntegrityError(
                        CandleIntegrityReason.INVALID_PRICE,
                        f"CandleDTO field '{price_field}' must be a finite Decimal, not bool",
                        timeframe=timeframe,
                    )
            for vol_field in ("volume", "quote_volume"):
                raw_vol = data.get(vol_field, NA)
                if isinstance(raw_vol, bool):
                    raise CandleIntegrityError(
                        CandleIntegrityReason.INVALID_VOLUME,
                        f"CandleDTO field '{vol_field}' must not be bool",
                        timeframe=timeframe,
                    )
            raw_trades = data.get("trade_count", NA)
            if isinstance(raw_trades, bool) or (
                raw_trades != NA and raw_trades is not None and not isinstance(raw_trades, int)
            ):
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_VOLUME,
                    "CandleDTO field 'trade_count' must be an integer or N/A",
                    timeframe=timeframe,
                )
        return data

    @model_validator(mode="after")
    def _validate_constructed_candle(self) -> CandleDTO:
        from app.data.candle_integrity import CandleIntegrityError, CandleIntegrityReason

        timeframe = str(self.interval or "unknown").strip().lower() or "unknown"
        for price_field in ("open", "high", "low", "close"):
            val = getattr(self, price_field)
            if not isinstance(val, Decimal) or not val.is_finite():
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_PRICE,
                    f"CandleDTO field '{price_field}' must be a finite Decimal",
                    timeframe=timeframe,
                )
        if not isinstance(self.volume, Decimal) or not self.volume.is_finite():
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_VOLUME,
                "CandleDTO field 'volume' must be a finite Decimal",
                timeframe=timeframe,
            )
        if self.quote_volume != NA and (
            not isinstance(self.quote_volume, Decimal) or not self.quote_volume.is_finite()
        ):
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_VOLUME,
                "CandleDTO field 'quote_volume' must be a finite Decimal or N/A",
                timeframe=timeframe,
            )
        return self


def _reraise_candle_integrity_error(exc: ValidationError) -> None:
    from app.data.candle_integrity import CandleIntegrityError

    for err in exc.errors():
        ctx_err = (err.get("ctx") or {}).get("error")
        if isinstance(ctx_err, CandleIntegrityError):
            raise ctx_err from exc


class TickerDTO(MarketDataDTO):
    timestamp: MaybeInt = NA
    last_price: Decimal
    high_price_24h: MaybeDecimal = NA
    low_price_24h: MaybeDecimal = NA
    volume_24h: MaybeDecimal = NA
    quote_volume_24h: MaybeDecimal = NA
    price_change_24h: MaybeDecimal = NA
    price_change_ratio_24h: MaybeDecimal = NA
    mark_price: MaybeDecimal = NA
    index_price: MaybeDecimal = NA
    bid_price: MaybeDecimal = NA
    ask_price: MaybeDecimal = NA


class FundingDTO(MarketDataDTO):
    timestamp: int
    funding_rate: Decimal
    mark_price: MaybeDecimal = NA
    next_funding_time: MaybeInt = NA


class OpenInterestDTO(MarketDataDTO):
    timestamp: MaybeInt = NA
    open_interest: Decimal
    notional_value: MaybeDecimal = NA
    unit: str = NA
