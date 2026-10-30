from __future__ import annotations

import asyncio
import json

import pytest

from app.microstructure.liquidation import (
    LiquidationPayloadError,
    WrongLiquidationContractTypeError,
    parse_binance_liquidation,
)
from app.microstructure.liquidation_service import LiquidationFlowService
from tests.test_liquidation_flow import BASE, _payload


def _nested_identity_payload(fields=("st", "ps")):
    payload = _payload(BASE)
    for key in fields:
        payload["o"][key] = payload.pop(key)
    return payload


@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("fields", [("st", "ps"), ("st",), ("ps",)])
def test_order_level_contract_identity_preserves_normalized_event(combined, fields) -> None:
    payload = _nested_identity_payload(fields)
    wire = {"stream": "!forceOrder@arr", "data": payload} if combined else payload
    assert parse_binance_liquidation(json.dumps(wire)) == parse_binance_liquidation(_payload(BASE))


def test_matching_identity_fields_at_both_locations_are_accepted() -> None:
    payload = _nested_identity_payload()
    payload.update(st=1, ps="BTCUSDT")
    assert parse_binance_liquidation(payload) == parse_binance_liquidation(_payload(BASE))


@pytest.mark.parametrize("field,value", [("st", 2), ("ps", "ETHUSDT")])
def test_conflicting_contract_identity_is_rejected(field, value) -> None:
    payload = _nested_identity_payload()
    payload[field] = value
    with pytest.raises(LiquidationPayloadError, match="conflicting"):
        parse_binance_liquidation(payload)


@pytest.mark.parametrize("field", ["st", "ps"])
def test_identity_missing_from_both_locations_is_rejected(field) -> None:
    payload = _nested_identity_payload()
    del payload["o"][field]
    with pytest.raises(LiquidationPayloadError):
        parse_binance_liquidation(payload)


@pytest.mark.parametrize("field,value", [
    ("st", None), ("st", True), ("st", "invalid"), ("st", 1.5), ("st", float("inf")),
    ("ps", None), ("ps", ""),
])
@pytest.mark.parametrize("location", ["event", "order"])
def test_invalid_identity_cannot_fall_back_to_valid_other_location(field, value, location) -> None:
    payload = _nested_identity_payload()
    payload.update(st=1, ps="BTCUSDT")
    target = payload if location == "event" else payload["o"]
    target[field] = value
    with pytest.raises(LiquidationPayloadError):
        parse_binance_liquidation(payload)


def test_nested_coin_m_identity_remains_rejected() -> None:
    payload = _nested_identity_payload()
    payload["o"]["st"] = 2
    with pytest.raises(WrongLiquidationContractTypeError):
        parse_binance_liquidation(payload)


class _Connection:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.reads = 0
        self.idle = asyncio.Event()
        self.closed = False

    async def send(self, payload):
        self.request = json.loads(payload)

    async def recv(self):
        self.reads += 1
        if self.reads == 1:
            return {"result": None, "id": self.request["id"]}
        if self.reads == 2:
            return {"stream": "!forceOrder@arr", "data": self.payload}
        self.idle.set()
        await asyncio.Future()

    async def close(self):
        self.closed = True


class _Transport:
    def __init__(self, connection):
        self.connection = connection

    async def connect(self):
        return self.connection


def test_nested_identity_does_not_create_a_protocol_failure_reconnect_loop() -> None:
    async def scenario():
        connection = _Connection(_nested_identity_payload())
        service = LiquidationFlowService(transport=_Transport(connection), clock=lambda: BASE)
        await service.start(["BTCUSDT"])
        try:
            # The third receive is reached only after ack and event processing.
            await asyncio.wait_for(connection.idle.wait(), 2)
            health = service.health()
            assert health["connected"] and health["subscription_confirmed"]
            assert health["accepted_market_event_count"] == 1
            assert health["malformed_event_count"] == health["disconnect_count"] == 0
            assert connection.request["params"] == ["!forceOrder@arr"]
        finally:
            await service.stop()
        assert connection.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("st", 2), ("ps", "ETHUSDT")])
def test_conflicting_identity_still_invalidates_connected_stream(field, value) -> None:
    async def scenario():
        payload = _payload(BASE)
        payload["o"].update(st=1, ps="BTCUSDT")
        payload["o"][field] = value
        connection = _Connection(payload)
        backing_off = asyncio.Event()

        async def sleep(delay):
            backing_off.set()
            await asyncio.Future()

        service = LiquidationFlowService(transport=_Transport(connection), clock=lambda: BASE, sleep=sleep)
        await service.start(["BTCUSDT"])
        try:
            await asyncio.wait_for(backing_off.wait(), 2)
            health = service.health()
            assert not health["connected"]
            assert not health["subscription_confirmed"]
            assert health["malformed_event_count"] == 1
            assert health["last_failure"]["category"] == "protocol_failure"
        finally:
            await service.stop()
        assert connection.closed

    asyncio.run(scenario())
