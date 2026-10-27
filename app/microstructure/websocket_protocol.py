"""Public stream control messages and bounded, credential-free failure facts."""
from __future__ import annotations

import json
import socket
import ssl
from collections.abc import Mapping
from typing import Any

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidProxy, ProxyError


class SubscriptionRejectedError(ConnectionError):
    pass


class ProviderShutdownError(ConnectionError):
    pass


class UnverifiedPayloadError(ValueError):
    pass


def matches_usdt_perpetual(symbol: str, pair_symbol: str | None = None) -> bool:
    """Check the literal stream identity; the requested universe proves listing metadata."""
    return (symbol.isascii() and symbol.isalnum() and symbol.endswith("USDT")
            and (pair_symbol is None or pair_symbol == symbol))


def is_subscription_ack(payload: str | bytes | Mapping[str, Any], request_id: int) -> bool:
    try:
        decoded = payload if isinstance(payload, Mapping) else json.loads(payload)
    except (ValueError, TypeError, UnicodeError):
        return False
    return (isinstance(decoded, Mapping) and type(decoded.get("id")) is int
            and decoded["id"] == request_id and "result" in decoded
            and decoded["result"] is None and "code" not in decoded and "error" not in decoded)


def is_control_message(payload: str | bytes | Mapping[str, Any]) -> bool:
    try:
        decoded = payload if isinstance(payload, Mapping) else json.loads(payload)
    except (ValueError, TypeError, UnicodeError):
        return False
    if not isinstance(decoded, Mapping):
        return False
    event = decoded.get("data", decoded)
    if isinstance(event, Mapping) and event.get("e") == "serverShutdown":
        raise ProviderShutdownError("binance_server_shutdown")
    if "e" not in decoded and "data" not in decoded:
        if "code" in decoded or "error" in decoded:
            error = decoded.get("error")
            code = error.get("code") if isinstance(error, Mapping) else decoded.get("code")
            safe_code = code if type(code) is int else "invalid"
            raise SubscriptionRejectedError(f"binance_subscription_rejected:code={safe_code}")
        return "id" in decoded
    return False


def failure_facts(exc: Exception, phase: str) -> dict[str, Any]:
    category = "connection_failure"
    if isinstance(exc, SubscriptionRejectedError):
        category = "subscription_rejected"
    elif isinstance(exc, ProviderShutdownError):
        category = "provider_shutdown"
    elif isinstance(exc, socket.gaierror):
        category = "dns_failure"
    elif isinstance(exc, ssl.SSLCertVerificationError):
        category = "tls_certificate_failure"
    elif isinstance(exc, ssl.SSLError):
        category = "tls_failure"
    elif isinstance(exc, (InvalidProxy, ProxyError)):
        category = "proxy_failure"
    elif isinstance(exc, InvalidStatus):
        category = "upgrade_rejected"
    elif isinstance(exc, (InvalidHandshake, UnverifiedPayloadError)):
        category = "protocol_failure"
    elif isinstance(exc, TimeoutError):
        category = {"connect": "handshake_timeout", "subscribe": "subscription_timeout"}.get(phase, "receive_timeout")
    elif isinstance(exc, ConnectionClosed):
        if exc.sent is not None and exc.sent.code == 1011 and "keepalive ping timeout" in exc.sent.reason:
            category = "heartbeat_timeout"
        else:
            category = "peer_close" if exc.rcvd is not None else "abrupt_close"
    elif isinstance(exc, OSError):
        category = "tcp_failure" if phase == "connect" else "network_failure"
    return {
        "category": category,
        "exception": type(exc).__name__,
        "phase": phase,
        "http_status": exc.response.status_code if isinstance(exc, InvalidStatus) else None,
        "received_close_code": exc.rcvd.code if isinstance(exc, ConnectionClosed) and exc.rcvd else None,
        "sent_close_code": exc.sent.code if isinstance(exc, ConnectionClosed) and exc.sent else None,
    }
