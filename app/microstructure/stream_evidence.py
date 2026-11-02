"""Bounded stream identity, acknowledgements and assessment diagnostics."""
from __future__ import annotations

import unicodedata
from collections import deque
from datetime import datetime

from app.microstructure.websocket_protocol import matches_usdt_perpetual, is_subscription_ack

METADATA_MAX_AGE_SECONDS = 300


class StreamEvidence:
    def __init__(self, clock, *, on_admission_change=None):
        self.clock = clock
        self._on_admission_change = on_admission_change
        self._desired = set()
        self._admission = {}
        self._latest_metadata_at = None
        self._latest_identity = None
        self.metadata_refresh = {}
        self.rejected_event_time_count = 0
        self.contracts = None
        self.metadata_at = None
        self.metadata_issue = None
        self.rejections = {}
        self.pending = {}
        self.subscription_versions = {}
        self.acknowledged = set()
        self.ack_count = 0
        self.request_count = 0
        self.decoded_count = 0
        self.symbols = {}
        self.history = deque(maxlen=32)

    def set_metadata(self, payload, acquired_at):
        # Observe the old lease BEFORE replacing it, including quiet streams
        # which received no events/assessments while metadata was expired.
        for symbol in self._desired:
            self.identity_reason(symbol)
        rows = payload.get("symbols", []) if isinstance(payload, dict) else []
        self.metadata_issue = None
        if not isinstance(rows, list) or not rows or len(rows) > 10000:
            rows = []
            self.metadata_issue = "malformed_or_empty_exchange_metadata"
        if not isinstance(acquired_at, datetime) or acquired_at.tzinfo is None:
            acquired_at = None
            self.metadata_issue = "metadata_acquisition_unverified"
        groups, contracts = {}, set()
        self.rejections = {}
        for row in rows[:10000]:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                continue
            symbol = row["symbol"]
            key = unicodedata.normalize("NFKC", symbol).casefold()
            groups.setdefault(key, []).append(symbol)
            if (matches_usdt_perpetual(symbol) and row.get("pair") == symbol
                    and row.get("baseAsset") == symbol[:-4] and row.get("quoteAsset") == "USDT"
                    and row.get("marginAsset") == "USDT" and row.get("contractType") == "PERPETUAL"
                    and row.get("status") == "TRADING"):
                contracts.add(symbol)
        for group in groups.values():
            if len(group) > 1:
                for symbol in group:
                    contracts.discard(symbol)
                    self.rejections[symbol] = "exchange_metadata_normalization_collision"
        self.contracts = frozenset(contracts)
        self.metadata_at = acquired_at
        identity = (self.contracts, tuple(sorted(self.rejections.items())))
        if acquired_at is not None:
            if acquired_at > self.clock():
                self.metadata_issue = "exchange_metadata_stale_or_future"
            elif self._latest_metadata_at is not None and acquired_at < self._latest_metadata_at:
                self.metadata_issue = "metadata_acquisition_regressed"
            elif acquired_at == self._latest_metadata_at and identity != self._latest_identity:
                self.metadata_issue = "conflicting_metadata_acquisition"
            if self.metadata_issue is None:
                self._latest_metadata_at, self._latest_identity = acquired_at, identity
        for symbol in self._desired:
            self.identity_reason(symbol)

    def identity_reason(self, symbol):
        reason = self._identity_reason(symbol)
        if self.contracts is not None and symbol in self._desired:
            valid = reason is None
            if self._admission.get(symbol) is not valid:
                self._admission[symbol] = valid
                if self._on_admission_change is not None:
                    self._on_admission_change(symbol, valid)
        return reason

    def _identity_reason(self, symbol):
        if self.contracts is None:
            return None  # Legacy/injected services expose unverified metadata in health.
        if self.metadata_issue:
            return self.metadata_issue
        if symbol not in self.contracts:
            return self.rejections.get(symbol, "not_verified_binance_usdm_contract")
        if self.metadata_at is None or self.metadata_at.tzinfo is None:
            return "metadata_acquisition_unverified"
        age = (self.clock()-self.metadata_at).total_seconds()
        return None if 0 <= age <= METADATA_MAX_AGE_SECONDS else "exchange_metadata_stale_or_future"

    def accepts_event_times(self, symbol, *timestamps, max_age):
        now = self.clock()
        if all(0 <= (now-at).total_seconds() <= max_age for at in timestamps):
            return True
        self.rejected_event_time_count += 1
        # An expired/future observation cannot contribute to a complete window.
        if self._on_admission_change is not None:
            self._on_admission_change(symbol, False)
            self._on_admission_change(symbol, True)
        return False

    def matches(self, symbol, pair=None):
        return matches_usdt_perpetual(symbol, pair) and self.identity_reason(symbol) is None

    def reset(self):
        self.pending.clear()
        self.subscription_versions.clear()
        self.acknowledged.clear()
        self.history.append({"event": "connection_invalidated", "at": self.clock().isoformat()})

    def requested(self, request_id, method, symbols):
        if len(self.pending) >= 128:
            self.pending.pop(next(iter(self.pending)))
        self.pending[request_id] = (method, frozenset(symbols))
        for symbol in symbols:
            self.subscription_versions[symbol] = request_id
        self.request_count += 1
        self.history.append({"event": method, "request_id": request_id,
                             "symbols": len(symbols), "at": self.clock().isoformat()})

    def control(self, payload):
        for request_id, (method, symbols) in tuple(self.pending.items()):
            if is_subscription_ack(payload, request_id):
                self.pending.pop(request_id)
                symbols = {s for s in symbols if self.subscription_versions.get(s) == request_id}
                if method == "SUBSCRIBE":
                    self.acknowledged.update(symbols)
                else:
                    self.acknowledged.difference_update(symbols)
                self.ack_count += 1
                self.history.append({"event": "acknowledged", "request_id": request_id,
                                     "at": self.clock().isoformat()})
                return True
        return False

    def retain(self, desired):
        self._desired = set(desired)
        self._admission = {s:v for s,v in self._admission.items() if s in desired}
        self.symbols = {s:v for s,v in self.symbols.items() if s in desired}
        self.subscription_versions = {s:v for s,v in self.subscription_versions.items() if s in desired}
        self.acknowledged.intersection_update(desired)
        for symbol in desired:
            self.identity_reason(symbol)

    def decoded(self, symbol, event_at, *, accepted):
        now = self.clock()
        record = self.symbols.setdefault(symbol, {"decoded_events": 0, "accepted_events": 0})
        record.update(source_at=event_at.isoformat(), received_at=now.isoformat(),
                      ingestion_delay_seconds=round((now-event_at).total_seconds(), 6))
        record["decoded_events"] += 1
        record["accepted_events"] += int(accepted)
        self.decoded_count += 1

    def assessed(self, symbol, snapshot):
        if snapshot is None:
            return
        record = self.symbols.setdefault(symbol, {"decoded_events": 0, "accepted_events": 0})
        bands = getattr(snapshot, "bands", None)
        record["assessment"] = {"assessed_at": self.clock().isoformat(), "status": snapshot.status.value,
            "source_at": snapshot.observed_at.isoformat() if snapshot.observed_at else None,
            "snapshot_age_seconds": snapshot.age_seconds,
            "reason": snapshot.reason, "synchronized": getattr(snapshot, "synchronized", None),
            "all_depth_bands_complete": all(b.bid_coverage_complete and b.ask_coverage_complete
                for b in bands.values()) if bands else None,
            "gap_count": getattr(snapshot, "gap_count", None),
            "resync_count": getattr(snapshot, "resync_count", None)}

    def health(self, desired):
        now = self.clock()
        metadata_status = ("Unverified" if self.contracts is None or self.metadata_at is None or self.metadata_issue
            else "VERIFIED" if 0 <= (now-self.metadata_at).total_seconds() <= METADATA_MAX_AGE_SECONDS else "STALE")
        return {"metadata_status": metadata_status,
            "metadata_refresh": dict(self.metadata_refresh),
            "rejected_event_time_count": self.rejected_event_time_count,
            "metadata_issue": self.metadata_issue,
            "metadata_acquired_at": self.metadata_at.isoformat() if self.metadata_at else None,
            "subscription_requested_count": self.request_count, "subscription_ack_count": self.ack_count,
            "pending_subscription_count": len(self.pending), "acknowledged_symbol_count": len(self.acknowledged),
            "valid_decoded_event_count": self.decoded_count, "recent_service_events": list(self.history),
            "queue_delay_seconds": "N/A: transport queue enqueue time is not exposed",
            "per_symbol": {s: {**self.symbols.get(s, {}), "subscription_acknowledged": s in self.acknowledged,
                "identity_status": self.identity_reason(s) or ("VERIFIED" if self.contracts is not None else "Unverified"),
                "source_event_age_seconds": round((now-datetime.fromisoformat(self.symbols[s]["source_at"])).total_seconds(), 3)
                    if "source_at" in self.symbols.get(s, {}) else None}
                for s in sorted(desired)[:1024]}}
