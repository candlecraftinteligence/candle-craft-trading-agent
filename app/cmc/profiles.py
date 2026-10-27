"""Acquisition schedules and resource bounds; never change source freshness gates."""
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.cmc.endpoints import CONTRACTS, Endpoint
from app.cmc.models import Frozen


class Family(Frozen):
    endpoint: Endpoint
    seconds: int = Field(ge=60, le=604800)
    max_pages: int = Field(default=1, ge=1, le=4)
    limit: int | None = Field(default=None, ge=1, le=5000)

    @model_validator(mode="after")
    def valid_route(self):
        if self.endpoint in {Endpoint.HISTORICAL_LISTINGS, Endpoint.KEY_INFO}:
            raise ValueError("explicit validation/replay routes cannot run in a collector profile")
        if self.limit is not None and self.limit > CONTRACTS[self.endpoint].page_max:
            raise ValueError("profile page limit exceeds source contract")
        return self


class CollectionProfile(Frozen):
    name: Literal["FULL_RESEARCH", "DECISION_FOCUSED", "LOW_CREDIT", "CUSTOM"]
    families: tuple[Family, ...]
    concurrency: int = Field(default=2, ge=1, le=4)
    requests_per_minute: int = Field(default=45, ge=1, le=50)
    cache_entries: int = Field(default=32, ge=16, le=64)
    max_body_bytes: int = Field(default=1024*1024, ge=1024, le=2*1024*1024)
    max_observations: int = Field(default=2500, ge=1, le=5000)
    retention_days: int = Field(default=30, ge=1, le=90)

    @model_validator(mode="after")
    def unique_families(self):
        if not 1 <= len(self.families) <= 13 or len({f.endpoint for f in self.families}) != len(self.families):
            raise ValueError("profile families must be bounded and unique")
        return self

    @property
    def tick_seconds(self):
        return min(f.seconds for f in self.families)

    def family(self, endpoint):
        return next((f for f in self.families if f.endpoint == endpoint), None)


def profile_for(name="FULL_RESEARCH", *, legacy_seconds=300, custom_path: Path | None = None):
    if name == "CUSTOM":
        if custom_path is None or custom_path.stat().st_size > 32768:
            raise ValueError("bounded custom profile file required")
        profile = CollectionProfile.model_validate_json(custom_path.read_text(encoding="utf-8"))
        if profile.name != "CUSTOM":
            raise ValueError("custom profile name required")
        return profile
    if name == "FULL_RESEARCH":
        return CollectionProfile(name=name, families=tuple(Family(endpoint=e, seconds=legacy_seconds,
            max_pages=2, limit=1000 if e == Endpoint.MAP else 250 if e in {
                Endpoint.EXCHANGE_PAIRS, Endpoint.CRYPTO_PAIRS} else 100 if CONTRACTS[e].paginated else None)
            for e in Endpoint if e not in {Endpoint.KEY_INFO, Endpoint.HISTORICAL_LISTINGS}))
    if name not in {"DECISION_FOCUSED", "LOW_CREDIT"}:
        raise ValueError("unknown CMC profile")
    cadence = 600 if name == "DECISION_FOCUSED" else 900
    return CollectionProfile(name=name, families=(
        Family(endpoint=Endpoint.GLOBAL, seconds=cadence),
        Family(endpoint=Endpoint.LIQUIDATIONS, seconds=cadence),
        Family(endpoint=Endpoint.LISTINGS, seconds=3600 if name == "DECISION_FOCUSED" else 21600, limit=100),
        Family(endpoint=Endpoint.ALTCOIN, seconds=3600 if name == "DECISION_FOCUSED" else 21600),
        Family(endpoint=Endpoint.MAP, seconds=86400, limit=1000, max_pages=2),
        Family(endpoint=Endpoint.EXCHANGES, seconds=86400, limit=100),
        Family(endpoint=Endpoint.EXCHANGE_PAIRS, seconds=86400, limit=250, max_pages=2)))
