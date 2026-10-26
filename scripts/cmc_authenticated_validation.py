"""Explicit authenticated capture or offline replay; never starts Scanner V1."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.cmc.authenticated_validation import analyze_capture
from app.cmc.client import CmcClient
from app.cmc.endpoints import Endpoint
from app.cmc.identity import AssetBinding
from app.cmc.models import encoded
from app.cmc.storage import Sidecar
from app.cmc.validation_capture import Capture, load_local_settings


def _bindings(path):
    if path.stat().st_size > 128*1024:
        raise ValueError("binding review size budget")
    review=json.loads(path.read_text(encoding="utf-8"))
    if review.get("validation_only") is not True:
        raise ValueError("validation-only identity review required")
    values=tuple(AssetBinding.model_validate(b) for b in review.get("bindings",[]))
    if not 1 <= len(values) <= 3:
        raise ValueError("validation cohort must contain one to three reviewed assets")
    return values


def _prior_probe_usage(path, now):
    if not path.exists():
        return 0,0
    if path.stat().st_size>128*1024:
        raise ValueError("prior probe size budget")
    probe=json.loads(path.read_text(encoding="utf-8"))
    usage=probe.get("usage",{})
    return (int(usage.get("d:"+now.strftime("%Y-%m-%d"),{}).get("reserved_credits",0)),
            int(usage.get("m:"+now.strftime("%Y-%m"),{}).get("reserved_credits",0)))


async def capture_sources(args):
    settings=load_local_settings(args.env_file)
    bindings=_bindings(args.bindings)
    now=datetime.now(UTC)
    previous_day,previous_month=_prior_probe_usage(args.prior_probe,now)
    store=Sidecar(args.ledger)
    client=CmcClient(store=store,api_key=settings.api_key,attempts=1,timeout=6,
        daily_credits=min(args.credit_cap,max(0,settings.daily_credits-previous_day)),
        monthly_credits=max(0,settings.monthly_credits-previous_month))
    session=Capture(args.source,client,max_native_requests=24)
    session.index["configured_budget"]={"daily":settings.daily_credits,"monthly":settings.monthly_credits}
    session.index["validation_credit_cap"]=args.credit_cap
    session.index["prior_probe_reserved"]={"daily":previous_day,"monthly":previous_month}
    try:
        if args.capture:
            ids=",".join(str(b.cmc_id) for b in bindings)
            symbols=",".join([b.binance_base for b in bindings]+["USDT"])
            requests=(
                (Endpoint.KEY_INFO,{}),(Endpoint.MAP,{"symbol":symbols,"limit":20}),
                (Endpoint.EXCHANGES,{"limit":100}),(Endpoint.FEAR_GREED,{}),(Endpoint.ALTCOIN,{}),
                (Endpoint.EXCHANGE_LIQUIDATIONS,{"exchange_slug":"binance","limit":5}),
                (Endpoint.CRYPTO_LIQUIDATIONS,{"crypto_id":ids,"limit":5}),
                *((Endpoint.CRYPTO_PAIRS,{"crypto_id":b.cmc_id,"category":"perpetual","matched_id":825,"limit":30}) for b in bindings),
                (Endpoint.CATEGORIES,{"limit":3}),
                (Endpoint.HISTORICAL_LISTINGS,{"date":(now-timedelta(days=1)).date().isoformat(),"limit":3}),
            )
            for endpoint,params in requests:
                fetched=await session.cmc_get(endpoint,params)
                if endpoint==Endpoint.CATEGORIES and isinstance(fetched.data,list) and fetched.data:
                    category=fetched.data[0].get("id")
                    if category:
                        await session.cmc_get(Endpoint.CATEGORY,{"id":category,"limit":3})
            for endpoint in ("/fapi/v1/exchangeInfo","/fapi/v1/fundingInfo","/fapi/v1/premiumIndex"):
                await session.native_get(endpoint)
            for binding in bindings:
                await session.native_get("/fapi/v1/fundingRate",{"symbol":binding.binance_base+"USDT","limit":3})
        start=len(session.index["sources"])
        await asyncio.gather(
            session.cmc_get(Endpoint.EXCHANGE_PAIRS,{"exchange_slug":"binance","category":"perpetual","matched_id":825,"limit":100},refresh=True),
            *(session.native_get(endpoint,{"symbol":b.binance_base+"USDT"}) for b in bindings
                for endpoint in ("/fapi/v1/openInterest","/fapi/v2/ticker/price")))
        group="concurrent_"+datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for record in session.index["sources"][start:]: record["sample_group"]=group
        for endpoint,params in ((Endpoint.GLOBAL,{}),(Endpoint.LIQUIDATIONS,{}),
                                (Endpoint.LISTINGS,{"limit":10}),(Endpoint.KEY_INFO,{})):
            await session.cmc_get(endpoint,params,refresh=True)
        session.save()
    finally:
        await session.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--capture",action="store_true",help="Collect all bounded source families")
    actions.add_argument("--refresh",action="store_true",help="Refresh matched metrics in an existing complete capture")
    actions.add_argument("--replay",action="store_true",help="No credential access or network")
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--bindings",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--env-file",type=Path,default=Path(".env"))
    parser.add_argument("--ledger",type=Path,default=Path("research_runs/cmc_authenticated_validation.sqlite"))
    parser.add_argument("--prior-probe",type=Path,default=Path("research_runs/cmc_authenticated.json"))
    parser.add_argument("--credit-cap",type=int,default=25)
    args=parser.parse_args(argv)
    if not 1<=args.credit_cap<=100:
        raise ValueError("validation credit cap must be between 1 and 100")
    if not args.replay:
        asyncio.run(asyncio.wait_for(capture_sources(args),timeout=180))
    result=analyze_capture(args.source,args.bindings)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(encoded(result)+"\n",encoding="utf-8")
    print(json.dumps({"output":str(args.output),"authenticated_routes":result["distinct_authenticated_routes"],
        "reported_credits":result["reported_validation_credits"],"comparisons":result["comparison_counts"]}))
    return result


if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        # Never print raw exception values from a credential/provider boundary.
        print(json.dumps({"validation_error_type":type(exc).__name__}))
        raise SystemExit(1)
