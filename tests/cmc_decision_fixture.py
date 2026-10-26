"""Fixed offline decisions for comparing an unchanged main checkout with the feature.

Run this file against either source root: python tests/cmc_decision_fixture.py OUTPUT.
No network transports, runtime database, listener, or live scanner are used.
"""
import asyncio
import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SOURCE_ROOT=Path.cwd()
sys.path.insert(0,str(SOURCE_ROOT))
sys.path.insert(0,str(SOURCE_ROOT/"tests"))

from app.pipeline.scanner_runner import ScannerRunner
from app.alerts.telegram_lifecycle import (
    telegram_alert_decision_for_symbol, _public_economic_setup_plan, _lifecycle_setup_delivery_signal_id,
)
from test_scanner_runner import FakeExchangeClient, _config, _strategy_pullback_candles, _flat_candles
from test_triggered_confirmed_telegram_delivery import _public_setup_symbol

AT=datetime(2026,9,21,tzinfo=UTC)


async def fixed_decisions():
    client=FakeExchangeClient({"BTCUSDT":_strategy_pullback_candles(),"ETHUSDT":_flat_candles()},
        failing_timeframes={"2d"},candle_as_of=AT)
    result=await ScannerRunner(exchange_client=client,clock=lambda:AT,capture_clock=lambda:AT).run(
        _config(["BTCUSDT","ETHUSDT"],decision_timestamp=AT))
    rows=[]
    for item in result.results:
        rows.append({name:getattr(item,name) for name in (
            "symbol","status","status_history","technical_score","derivatives_score","rejection_reason",
            "rejection_stage","valid_strategy_modes","rejected_strategy_modes","candidate_quality_grade",
            "final_quality_grade","final_failed_gate","bos_detected","choch_detected","sweep_detected")})
        rows[-1]["trade_plan"]=item.trade_idea.model_dump(mode="json") if item.trade_idea else None
    public=_public_setup_symbol(signal_id="synthetic:fixed-generation")
    decision=telegram_alert_decision_for_symbol(public)
    return {"synthetic_fixture_only":True,"config":result.config.model_dump(mode="json"),"scanner_decisions":rows,
        "public_decision":{"eligible":decision.eligible,"reason":decision.reason,"alert_type":decision.alert_type.value if decision.alert_type else None,
            "public_economic_plan":dataclasses.asdict(_public_economic_setup_plan(public,decision.message)) if decision.message else None,
            "delivery_signal_id":_lifecycle_setup_delivery_signal_id(public)}}


if __name__=="__main__":
    Path(sys.argv[1]).write_text(json.dumps(asyncio.run(fixed_decisions()),indent=2,sort_keys=True,default=str)+"\n",encoding="utf-8")
