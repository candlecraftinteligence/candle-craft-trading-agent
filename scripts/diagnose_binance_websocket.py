"""Bounded public-data probe. Never starts CCI or reads settings / databases.

Run from the Runtime checkout using its existing Python environment. Output is
sanitized JSON; no headers, proxy addresses, payloads, or environment values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.request import getproxies

import httpx
import websockets
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

# Supports copying this standalone file to %TEMP% on Runtime: run with the
# existing Runtime checkout as the current directory, without editing its source.
sys.path.insert(0, str(Path.cwd() if (Path.cwd() / "app/microstructure").is_dir()
                       else Path(__file__).resolve().parents[1]))
from app.microstructure.agg_trade import parse_binance_agg_trade
from app.microstructure.liquidation import parse_binance_liquidation
from app.microstructure.order_book import (
    SynchronizedLocalOrderBook, parse_binance_depth_event, parse_binance_depth_snapshot,
)

HOST = "fstream.binance.com"
PUBLIC_URL = f"wss://{HOST}/public/stream"
MARKET_URL = f"wss://{HOST}/market/stream"
PUBLIC_STREAMS = ["btcusdt@depth@500ms"]
MARKET_STREAMS = ["btcusdt@aggTrade", "!forceOrder@arr"]


def failure(exc: Exception, phase: str) -> dict[str, Any]:
    category = phase + "_failure"
    if isinstance(exc, socket.gaierror):
        category = "dns_failure"
    elif isinstance(exc, ssl.SSLCertVerificationError):
        category = "tls_certificate_failure"
    elif isinstance(exc, ssl.SSLError):
        category = "tls_failure"
    elif isinstance(exc, InvalidStatus):
        category = "http_upgrade_rejected"
    elif isinstance(exc, TimeoutError):
        category = phase + "_timeout"
    elif isinstance(exc, ConnectionClosed):
        # No close frame is transport loss, not proof of provider intent.
        category = "peer_close_frame" if exc.rcvd is not None else "transport_lost_without_close_frame"
    result: dict[str, Any] = {"category": category, "exception": type(exc).__name__}
    if isinstance(exc, InvalidStatus):
        result["http_status"] = exc.response.status_code
    if isinstance(exc, httpx.HTTPStatusError):
        result["http_status"] = exc.response.status_code
    if isinstance(exc, ConnectionClosed):
        result["received_close_code"] = exc.rcvd.code if exc.rcvd else None
        result["sent_close_code"] = exc.sent.code if exc.sent else None
    return result


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 3)
    except (OSError, TimeoutError):
        pass


async def network_stages() -> dict[str, Any]:
    result: dict[str, Any] = {"route": "direct; WebSocket proxy mode reported separately"}
    try:
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            HOST, 443, type=socket.SOCK_STREAM), 5)
        result["dns"] = {"status": "PASS", "addresses": sorted({item[4][0] for item in addresses})}
    except Exception as exc:
        result["dns"] = failure(exc, "dns")
        return result
    for tls in (False, True):
        stage = "tls" if tls else "tcp"
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(
                HOST, 443, ssl=ssl.create_default_context() if tls else None,
            ), 10)
            result[stage] = {"status": "PASS"}
            if tls:
                negotiated = writer.get_extra_info("ssl_object")
                result[stage].update(version=negotiated.version(),
                                     cipher=negotiated.cipher()[0], certificate_validated=True)
            await close_writer(writer)
        except Exception as exc:
            result[stage] = failure(exc, stage)
    return result


async def public_instruments(client: httpx.AsyncClient) -> set[str]:
    response = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    response.raise_for_status()
    return {row["symbol"] for row in response.json()["symbols"]
            if row.get("contractType") == "PERPETUAL" and row.get("quoteAsset") == "USDT"
            and row.get("status") == "TRADING"}


async def depth_snapshot(client: httpx.AsyncClient) -> Any:
    response = await client.get("https://fapi.binance.com/fapi/v1/depth",
                                params={"symbol": "BTCUSDT", "limit": 500})
    response.raise_for_status()
    return parse_binance_depth_snapshot(response.json())


async def probe_stream(url: str, streams: list[str], *, seconds: float, direct: bool,
                       client: httpx.AsyncClient, instruments: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url, "streams": streams, "frames": 0,
                            "valid": {}, "stale": 0, "unverified": 0, "coin_m_rejected": 0,
                            "subscription_ack": False, "max_probe_loop_lag_ms": 0.0,
                            "source_time_ranges": {}}
    book = SynchronizedLocalOrderBook("BTCUSDT", stale_after_seconds=5, event_buffer_size=256)
    connection = None
    snapshot_task = None
    phase = "opening_handshake"
    started = time.monotonic()
    try:
        connection = await connect(url, open_timeout=10, close_timeout=3,
                                   ping_interval=20, ping_timeout=20, max_queue=4,
                                   max_size=1024 * 1024, **({"proxy": None} if direct else {}))
        result.update(http_status=connection.response.status_code, protocol="HTTP/1.1 RFC6455",
                      subprotocol=connection.subprotocol, handshake_ms=round((time.monotonic()-started)*1000, 2))
        phase = "ping_pong"
        pong = await connection.ping()
        result["ping_rtt_ms"] = round(await asyncio.wait_for(pong, 5) * 1000, 2)
        await connection.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))
        book.mark_connected(reconnect=False)
        if streams == PUBLIC_STREAMS:
            snapshot_task = asyncio.create_task(depth_snapshot(client))
        phase = "receive"
        deadline = time.monotonic() + seconds
        installed = False
        while time.monotonic() < deadline:
            # Yield even under high traffic; this measures this probe, not CCI's loop.
            before = time.monotonic()
            await asyncio.sleep(0)
            result["max_probe_loop_lag_ms"] = max(result["max_probe_loop_lag_ms"],
                                                    round((time.monotonic()-before)*1000, 3))
            if snapshot_task is not None and snapshot_task.done() and not installed:
                try:
                    book.install_snapshot(snapshot_task.result(), received_at=datetime.now(UTC))
                    result["snapshot_acquired"] = True
                except Exception as exc:
                    result["snapshot_error"] = failure(exc, "rest_snapshot")
                installed = True
            try:
                payload = await asyncio.wait_for(connection.recv(), min(1, max(deadline-time.monotonic(), 0.001)))
            except TimeoutError:
                continue
            result["frames"] += 1
            try:
                decoded = json.loads(payload)
                if "id" in decoded:
                    if decoded.get("code") is not None or ("result" in decoded and decoded["result"] is not None):
                        result["subscription_error"] = {"code": decoded.get("code")}
                        break
                    result["subscription_ack"] = decoded.get("result", "missing") is None
                    continue
                event = decoded.get("data", decoded)
                if event.get("st") == 2:
                    result["coin_m_rejected"] += 1
                    continue
                event_type = event.get("e")
                if event_type == "depthUpdate":
                    parsed = parse_binance_depth_event(decoded)
                    if parsed.symbol != "BTCUSDT" or parsed.pair_symbol != parsed.symbol:
                        raise ValueError("unexpected depth instrument")
                elif event_type == "aggTrade":
                    parsed = parse_binance_agg_trade(decoded)
                elif event_type == "forceOrder":
                    parsed = parse_binance_liquidation(decoded)
                    if parsed.pair_symbol != parsed.symbol:
                        raise ValueError("unexpected liquidation pair")
                else:
                    raise ValueError("unexpected market event")
                if parsed.symbol not in instruments:
                    raise ValueError("unverified USDT perpetual identity")
                source_range = result["source_time_ranges"].setdefault(event_type, {})
                source_range.setdefault("first", parsed.event_time.isoformat())
                source_range["last"] = parsed.event_time.isoformat()
                if event_type == "depthUpdate":
                    book.ingest(parsed, received_at=datetime.now(UTC))
                age = (datetime.now(UTC) - parsed.event_time).total_seconds()
                if age > (30 if event_type == "forceOrder" else 5) or age < -1:
                    result["stale"] += 1
                else:
                    result["valid"][event_type] = result["valid"].get(event_type, 0) + 1
            except (ValueError, TypeError, KeyError, AttributeError):
                result["unverified"] += 1
        if streams == PUBLIC_STREAMS:
            snapshot = book.snapshot(as_of=datetime.now(UTC))
            result.update(order_book_synchronized_during_probe=book.synchronized,
                          book_status_at_end=snapshot.status.value, book_reason=snapshot.reason,
                          sequence_gaps=book.gap_count, buffer_overflows=book.buffer_overflow_count)
        await connection.send(json.dumps({"method": "UNSUBSCRIBE", "params": streams, "id": 2}))
    except Exception as exc:
        result["error"] = failure(exc, phase)
    finally:
        book.mark_disconnected()
        if snapshot_task is not None:
            snapshot_task.cancel()
            await asyncio.gather(snapshot_task, return_exceptions=True)
        if connection is not None:
            try:
                await connection.close()
            except Exception as exc:
                result["close_error"] = failure(exc, "close")
            result["final_close_code"] = connection.close_code
        result["elapsed_seconds"] = round(time.monotonic()-started, 2)
    return result


async def run(seconds: float, direct: bool, reconnect_test: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"observed_at": datetime.now(UTC).isoformat(),
        "python": sys.version.split()[0], "websockets": websockets.__version__,
        "proxy_mode": "direct" if direct else "library default / system proxy",
        "proxy_configured": {key: bool(value) for key, value in getproxies().items()},
        "certificate_override_present": {key: bool(os.environ.get(key)) for key in
                                         ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")},
        "scope": "standalone probe only; no scanner/listener/database access"}
    result["network"] = await network_stages()
    async with httpx.AsyncClient(timeout=10, trust_env=not direct) as client:
        try:
            instruments = await public_instruments(client)
            result["usdt_perpetual_metadata_count"] = len(instruments)
        except Exception as exc:
            result["metadata_error"] = failure(exc, "instrument_metadata")
            instruments = set()
        result["probes"] = []
        for url, streams in ((PUBLIC_URL, PUBLIC_STREAMS), (MARKET_URL, MARKET_STREAMS)):
            result["probes"].append(await probe_stream(url, streams, seconds=seconds, direct=direct,
                                                       client=client, instruments=instruments))
        if reconnect_test:
            # Close and recreate only this diagnostic socket. One retry, no loop.
            await asyncio.sleep(2)
            result["diagnostic_socket_reconnect"] = await probe_stream(
                PUBLIC_URL, PUBLIC_STREAMS, seconds=seconds, direct=direct, client=client, instruments=instruments)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=15, help="Receive window per socket, 5-30 seconds")
    parser.add_argument("--direct", action="store_true", help="Explicitly compare without system proxy")
    parser.add_argument("--reconnect-test", action="store_true", help="Recreate this probe's depth socket once")
    args = parser.parse_args()
    if not 5 <= args.seconds <= 30:
        parser.error("--seconds must be between 5 and 30")
    print(json.dumps(asyncio.run(run(args.seconds, args.direct, args.reconnect_test)), indent=2))


if __name__ == "__main__":
    main()
