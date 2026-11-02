"""Bounded offline synthetic collector probe. No credentials, live DB or network.

Run as a fresh process for an OS peak RSS receipt. The database is always a new
temporary CMC sidecar. Output is evidence about this DEV process only.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

import httpx
from pydantic import SecretStr
from app.cmc.client import CmcClient
from app.cmc.collection import Collector, IdentityManifest
from app.cmc.endpoints import Endpoint as E
from app.cmc.identity import AssetBinding
from app.cmc.profiles import profile_for
from app.cmc.storage import Sidecar
from app.core.process_memory import read_process_rss


def process_peak():
    if os.name == "nt":
        from app.core.process_memory import _ProcessMemoryCounters, _windows_memory_apis
        kernel, psapi = _windows_memory_apis()
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"peak_rss_bytes":int(counters.peak_working_set_size),
            "peak_process_commit_bytes":int(counters.peak_pagefile_usage),
            "source":"Windows GetProcessMemoryInfo; process commit is not physical pagefile occupancy"}
    import resource
    return {"peak_rss_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1024 if sys.platform.startswith("linux") else 1),
        "peak_process_commit_bytes":"N/A", "source":"getrusage ru_maxrss"}


async def synthetic_probe(*, pair_count=820, padding=1100):
    if not 751 <= pair_count <= 1000 or not 0 <= padding <= 1600:
        raise ValueError("probe bounded to four <=250-row pages and <=1600 padding chars per row")
    at = datetime(2026,1,1,tzinfo=UTC)
    baseline = read_process_rss().rss_bytes
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="cci-synthetic-cmc-") as folder:
        store = Sidecar(Path(folder)/"cmc_synthetic.sqlite")
        metadata, assets, pairs, bindings = [], [], [], []
        for i in range(1,pair_count+1):
            base = "币安人生" if i == 55 else f"SYNTHETIC{i}"
            pair = {"market_id":10000+i,"category":"perpetual","outlier_detected":False,"exclusions":None,
                "market_pair_base":{"crypto_id":i,"symbol":base,"exchange_symbol":base},
                "market_pair_quote":{"crypto_id":825,"symbol":"USDT","exchange_symbol":"USDT"},
                "exchange_reported_quotes":[{"convert_id":2781,"convert_symbol":"USD","price":100.125,
                    "index_price":99.125,"funding_rate":0.0001,"open_interest":50.5,"last_updated":at.isoformat()}],
                "synthetic_padding":"x"*padding}
            pairs.append(pair)
            if i <= 55:
                assets.append({"id":i,"slug":f"synthetic-{i}","is_active":1})
                metadata.append({"symbol":base+"USDT","baseAsset":base,"quoteAsset":"USDT","marginAsset":"USDT",
                    "contractType":"PERPETUAL","status":"TRADING"})
                bindings.append(AssetBinding(cmc_id=i,cmc_slug=f"synthetic-{i}",binance_base=base,
                    contract_symbol=base+"USDT",exchange_id=270,market_id=10000+i,
                    acquired_at=at-timedelta(hours=3),reviewed_at=at-timedelta(hours=2),approved_at=at-timedelta(hours=1),
                    expires_at=at+timedelta(days=1),approved=True,evidence_ref="SYNTHETIC-ONLY",
                    owner_approval_ref="SYNTHETIC-ONLY"))
        calls, maximum_body = 0, 0
        def handler(request):
            nonlocal calls, maximum_body
            calls += 1
            e = E(request.url.path)
            if e == E.MAP: data = assets
            elif e == E.EXCHANGES: data = {"exchanges":[{"exchange_id":270,"exchange_slug":"binance"}]}
            elif e == E.EXCHANGE_PAIRS:
                start = int(request.url.params["start"])-1
                data = {"exchange_id":270,"exchange_slug":"binance","market_pairs":pairs[start:start+250],
                    "has_more":start+250<pair_count}
            elif e == E.GLOBAL:
                data = {"btc_dominance":60,"eth_dominance":15,"last_updated":at.isoformat(),
                    "quote":{"USD":{"total_market_cap":100,"total_market_cap_yesterday_percentage_change":1,"last_updated":at.isoformat()}}}
            else: data = {"quotes":[]}
            response = httpx.Response(200,json={"status":{"error_code":0,"credit_count":0 if e == E.MAP else 1},"data":data})
            maximum_body = max(maximum_body,len(response.content))
            return response
        class Metadata:
            async def snapshot(self): return {"symbols":metadata}
        profile = profile_for("CUSTOM",custom_path=ROOT/"config/cmc/combined-shadow.collection.json")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            def collector():
                client = CmcClient(store=store,http_client=http,api_key=SecretStr("SYNTHETIC-NOT-A-CREDENTIAL"),
                    clock=lambda:at,max_entries=16,max_body_bytes=profile.max_body_bytes)
                return Collector(client,store,manifest=IdentityManifest(bindings=tuple(bindings)),
                    profile=profile,metadata_provider=Metadata())
            first = collector()
            snapshot = await first.collect()
            await first.client.aclose()
            calls_before = calls
            restarted = collector()
            restored = await restarted.collect()
            assert restored.observations == snapshot.observations, {
                "first_count":len(snapshot.observations),"restored_count":len(restored.observations),
                "first_difference":next(({k:(a.model_dump()[k],b.model_dump()[k]) for k in a.model_dump()
                    if a.model_dump()[k] != b.model_dump()[k]} for a,b in zip(snapshot.observations,restored.observations)
                    if a != b),None)}
            assert calls == calls_before, "restart spent credits instead of restoring complete family"
            await restarted.client.aclose()
        current, peak = tracemalloc.get_traced_memory()
        largest = max((t.size for t in tracemalloc.take_snapshot().traces),default=0)
        tracemalloc.stop()
        with store.connect() as db:
            records, payload = db.execute("SELECT records,bytes FROM storage_stats").fetchone()
            largest_record = db.execute("SELECT MAX(length(CAST(payload AS BLOB))) FROM evidence").fetchone()[0]
        return {"label":"OFFLINE SYNTHETIC DEV ONLY; not Runtime capacity proof", "pair_rows":pair_count,
            "eligible_synthetic_symbols":55,"pages":4,"http_calls":calls,"real_network_calls":0,
            "restart_additional_http_calls":calls-calls_before,"observations":len(snapshot.observations),
            "errors":list(snapshot.errors),"max_http_body_bytes":maximum_body,
            "rss_before_workload_bytes":baseline,"rss_after_workload_bytes":read_process_rss().rss_bytes,
            **process_peak(),"tracemalloc_peak_bytes":peak,"tracemalloc_live_bytes":current,
            "largest_live_traced_block_bytes":largest,"sqlite_file_bytes":store.path.stat().st_size,
            "sqlite_payload_bytes":payload,"sqlite_records":records,"largest_record_bytes":largest_record,
            "sqlite_max_bytes":store.max_bytes,"record_cap_bytes":2*1024*1024,
            "runtime_resource_readiness":"UNVERIFIED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-count",type=int,default=820)
    parser.add_argument("--padding",type=int,default=1100)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(synthetic_probe(pair_count=args.pair_count,padding=args.padding)),indent=2))


if __name__ == "__main__":
    main()
