"""Bounded DEV source capture; no scanner, listener or production DB access."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.cmc.client import CmcClient
from app.cmc.endpoints import Endpoint
from app.cmc.storage import Sidecar
from app.cmc.validation_capture import Capture, load_local_settings


async def capture(args):
    settings = load_local_settings(args.env_file)
    store = Sidecar(args.ledger)
    client = CmcClient(store=store, api_key=settings.api_key, attempts=1, timeout=6,
        daily_credits=min(args.credit_cap, settings.daily_credits),
        monthly_credits=min(args.credit_cap, settings.monthly_credits))
    session = Capture(args.source, client, max_native_requests=5)
    session.index["purpose"] = "DEV bounded real source evidence; not production acceptance"
    session.index["credit_cap"] = args.credit_cap
    try:
        for endpoint, params in ((Endpoint.KEY_INFO, {}),
            (Endpoint.MAP, {"symbol": "BTC,ETH,SOL,XRP,USDT", "limit": 100}),
            (Endpoint.EXCHANGES, {"limit": 100}),
            (Endpoint.EXCHANGE_PAIRS, {"exchange_slug": "binance", "category": "perpetual", "limit": 250}),
            (Endpoint.GLOBAL, {}), (Endpoint.LIQUIDATIONS, {})):
            await session.cmc_get(endpoint, params)
        for endpoint in ("/fapi/v1/exchangeInfo", "/fapi/v1/fundingInfo", "/fapi/v1/premiumIndex"):
            await session.native_get(endpoint)
        # One authoritative provider identity record to investigate XRP collisions.
        async with httpx.AsyncClient(timeout=6, follow_redirects=False) as http:
            async with http.stream("GET", "https://api.coinpaprika.com/v1/coins/xrp-xrp") as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 512*1024:
                        raise ValueError("CoinPaprika identity byte budget")
                record = {"provider": "CoinPaprika", "endpoint": "/v1/coins/xrp-xrp",
                    "acquired_at": datetime.now(UTC).isoformat(), "http_status": response.status_code,
                    "error": None if response.status_code == 200 else "http_"+str(response.status_code)}
                if response.status_code == 200:
                    try:
                        identity = json.loads(body)
                        if (not isinstance(identity, dict) or identity.get("id") != "xrp-xrp"
                                or identity.get("symbol") != "XRP" or identity.get("is_active") is not True):
                            raise ValueError("identity contract")
                    except (ValueError, TypeError):
                        record["error"] = "identity_response_contract"
                    session._persist(record, bytes(body))
                session.index["sources"].append(record)
    finally:
        session.save()
        await session.close()
    return session.index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--credit-cap", type=int, default=8, choices=range(1, 11))
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(capture(args), timeout=90))
    print(json.dumps({"sources": len(result["sources"]), "credit_cap": args.credit_cap,
        "routes": [{k:r.get(k) for k in ("provider", "endpoint", "error", "authenticated", "credits_reported")}
            for r in result["sources"]], "usage": result.get("usage")}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}))
        raise SystemExit(1)
