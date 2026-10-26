"""Release boundary regressions: modes, selection provenance and trusted economics."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.cmc import integration, selection_audit
from app.cmc.models import Mode
from app.cmc.policy import evaluate, rank
from app.cmc.research import Outcome, ResearchRow, compare_policies
from app.cmc.storage import Sidecar
from app.core.config import Settings
from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
from tests.fixtures.cmc import AT, approval, candidate, feature, obs
from test_cmc_integration import FrozenNow, setup_symbol, stress_snapshot
from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender, _run_result, _run_result_many, _with_lifecycle_fields,
)
from test_triggered_confirmed_telegram_delivery import _public_setup_symbol


@pytest.mark.parametrize("mode", ["off", "collect"])
def test_mode_switch_removes_existing_decision_authority(tmp_path, monkeypatch, mode):
    async def scenario():
        monkeypatch.setattr(integration, "datetime", FrozenNow)
        evaluation = evaluate(candidate(), (feature(),), mode=Mode.DECISION, as_of=AT,
                              activations=(approval(),))
        symbol = setup_symbol().model_copy(update={"cmc_evaluation": evaluation})
        assert integration.veto_applies(symbol, alert_type="SIGNAL_CONFIRMED", previously_public=False)
        result = _run_result(symbol)
        settings = Settings(_env_file=None, cmc_mode=mode)
        cleared = await integration.prepare_selection(result, settings, None)
        assert cleared.results[0].cmc_evaluation is None
        sender = FakeSender()
        delivery = TelegramLifecycleDeliveryService(database_path=tmp_path/"public.db", settings=settings, sender=sender)
        assert (await delivery.deliver_for_run(result)).sent == 1
        assert len(sender.messages) == 1
    asyncio.run(scenario())


def test_invalid_boundary_and_absent_service_discard_old_authority(tmp_path, monkeypatch):
    async def scenario():
        old = evaluate(candidate(), (feature(),), mode=Mode.DECISION, as_of=AT, activations=(approval(),))
        result = _run_result(setup_symbol().model_copy(update={"cmc_evaluation": old}))
        settings = Settings(_env_file=None, cmc_mode="decision")
        result = result.model_copy(update={"config": result.config.model_copy(update={"decision_timestamp":AT})})
        async def absent(_): return None
        monkeypatch.setattr(integration, "service_for", absent)
        for cutoff in (AT-timedelta(seconds=1), AT):
            output = await integration.evaluate_delivery_run(result, settings,
                candidate_builder=lambda _: candidate(), decision_at=cutoff)
            assert output.results[0].cmc_evaluation is None
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["shadow", "decision"])
def test_actual_delivery_has_separate_policy_intent_and_application(tmp_path, monkeypatch, mode):
    async def scenario():
        store = Sidecar(tmp_path/"cmc_audit.sqlite")
        fake = SimpleNamespace(store=store, snapshot=stress_snapshot())
        async def service(_): return fake
        monkeypatch.setattr(integration, "service_for", service)
        monkeypatch.setattr(integration, "datetime", FrozenNow)
        manifest = tmp_path/"rules.json"
        manifest.write_text(json.dumps([approval("liquidation_stress").model_dump(mode="json")]))
        settings = Settings(_env_file=None, cmc_mode=mode, cmc_activation_manifest=manifest)
        result = _run_result(setup_symbol())
        result = result.model_copy(update={"config": result.config.model_copy(update={"decision_timestamp":AT})})
        sender = FakeSender()
        summary = await TelegramLifecycleDeliveryService(database_path=tmp_path/"public.db",
            settings=settings, sender=sender).deliver_for_run(result, scan_run_id="synthetic:audit")
        intents = store.causal("selection_intent", AT)
        applications = store.causal("selection_application", AT)
        policy = store.causal("policy_batch", AT)[0][0]
        assert policy["context"]["trading_style"] == result.results[0].lifecycle_state.mode
        assert policy["context"]["market_regime"] == "N/A"
        assert policy["context"]["market_regime_status"] == "MISSING"
        assert policy["context"]["cci_original_decision_at"] == AT.isoformat()
        assert len(intents) == len(applications) == 1
        assert intents[0]["evaluations"][0]["vetoed"] == (mode == "decision")
        assert intents[0]["evaluations"][0]["hypothetical_veto"] is True
        observed = applications[0]["deliveries"]
        assert any(d["cmc_veto_observed"] for d in observed) == (mode == "decision")
        assert summary.sent == (0 if mode == "decision" else 1)
        assert applications[0]["fills"] == applications[0]["economic_outcomes"] == "N/A"
        for flags,status in (({"enabled":False,"missing_data":("synthetic:gap",),"unverified_data":()},"DISABLED"),
                             ({"missing_data":("synthetic:gap",),"unverified_data":()},"MISSING"),
                             ({"missing_data":(),"unverified_data":("synthetic:unverified",)},"UNVERIFIED"),
                             ({"missing_data":(),"unverified_data":()},"VERIFIED")):
            changed=result.model_copy(update={"market_regime":result.market_regime.model_copy(update={"enabled":True,**flags})})
            await integration.evaluate_delivery_run(changed,settings,service=fake,
                candidate_builder=lambda _:candidate(),decision_at=AT)
            contexts=[r[0]["context"] for r in store.causal("policy_batch",AT)]
            matching=next(c for c in contexts if c["market_regime_status"]==status)
            assert matching["market_regime"] == ("Unverified" if status=="UNVERIFIED"
                else "N/A" if status in {"MISSING","DISABLED"} else changed.market_regime.state.value)
    asyncio.run(scenario())


def test_selection_audit_failure_removes_enforcement_before_send(tmp_path, monkeypatch):
    async def scenario():
        real_store = Sidecar(tmp_path/"cmc_failure.sqlite")
        class IntentFailure:
            def put(self, kind, *args, **kwargs):
                if kind == "selection_intent": raise OSError("synthetic disk failure")
                return real_store.put(kind, *args, **kwargs)
        fake = SimpleNamespace(store=IntentFailure(), snapshot=stress_snapshot())
        async def service(_): return fake
        monkeypatch.setattr(integration, "service_for", service)
        monkeypatch.setattr(integration, "datetime", FrozenNow)
        manifest = tmp_path/"rules.json"
        manifest.write_text(json.dumps([approval("liquidation_stress").model_dump(mode="json")]))
        result = _run_result(setup_symbol())
        result = result.model_copy(update={"config":result.config.model_copy(update={"decision_timestamp":AT})})
        settings = Settings(_env_file=None, cmc_mode="decision", cmc_activation_manifest=manifest)
        summary = await TelegramLifecycleDeliveryService(database_path=tmp_path/"public.db",
            settings=settings, sender=FakeSender()).deliver_for_run(result)
        assert summary.sent == 1
        assert not real_store.causal("selection_application", AT)
    asyncio.run(scenario())


def test_adverse_regime_tie_break_preserves_quality_and_rejected_candidates():
    features = (feature("CmcGlobalMarketRisk", 4), feature("CmcLiquidationPressure", 3))
    def assessment(symbol, direction, score=-88, eligible=True):
        return evaluate(candidate(symbol=symbol, direction=direction, economic_plan_id=symbol,
            canonical_rank=(Decimal(score),), cci_eligible=eligible), features,
            mode=Mode.DECISION, as_of=AT, activations=(approval("adverse_regime"),))
    long, short = assessment("AAA", "long"), assessment("ZZZ", "short")
    assert long.priority_delta == -1 and short.priority_delta == 0
    assert rank((long, short)) == (short, long)
    better = assessment("BETTER", "long", -89)
    rejected = assessment("REJECTED", "short", -100, False)
    assert rank((short, better, rejected))[0] == better
    assert rejected.final_eligible is False
    assert not evaluate(long.candidate, features, mode=Mode.DECISION, as_of=AT).priority_delta
    stale = tuple(f.model_copy(update={"input_timestamps":(AT-timedelta(seconds=301),)}) for f in features)
    assert evaluate(long.candidate, stale, mode=Mode.DECISION, as_of=AT,
                    activations=(approval("adverse_regime"),)).priority_delta == 0


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("long_quality", [88, 89])
def test_public_priority_only_changes_real_confirmed_ties(tmp_path, monkeypatch, mode, long_quality):
    async def scenario():
        snapshot = stress_snapshot()
        snapshot = snapshot.model_copy(update={"observations":(*snapshot.observations,obs("market_cap_change_24h","-4"))})
        store = Sidecar(tmp_path/"cmc_ranking.sqlite")
        async def service(_): return SimpleNamespace(store=store,snapshot=snapshot)
        monkeypatch.setattr(integration,"service_for",service)
        monkeypatch.setattr(integration,"datetime",FrozenNow)
        manifest = tmp_path/"rules.json"
        manifest.write_text(json.dumps([approval("adverse_regime").model_dump(mode="json")]))
        symbols = []
        for symbol,side,quality in (("AAAUSDT","long",long_quality),("ZZZUSDT","short",88)):
            s = _public_setup_symbol(signal_id="synthetic:"+symbol,direction=side,
                stop_loss=Decimal(95 if side=="long" else 107))
            s = _with_lifecycle_fields(s,symbol=symbol,quality_score=quality,
                setup_id="synthetic:setup:"+symbol,plan_version_id="synthetic:plan:"+symbol)
            symbols.append(s.model_copy(update={"symbol":symbol,
                "setup_quality":s.setup_quality.model_copy(update={"quality_score":quality})}))
        result = _run_result_many(*symbols)
        result = result.model_copy(update={"config":result.config.model_copy(update={"decision_timestamp":AT})})
        before = result.model_dump(mode="json")
        settings = Settings(_env_file=None,cmc_mode=mode.value,cmc_activation_manifest=manifest,
            telegram_public_watchlist_enabled=True,public_watchlist_max_per_scan=1)
        summary = await TelegramLifecycleDeliveryService(database_path=tmp_path/"public.db",
            settings=settings,sender=FakeSender()).deliver_for_run(result)
        sent = [d for d in summary.deliveries if d.status=="sent"]
        assert len(sent)==2, [(d.symbol,d.status,d.error_message) for d in summary.deliveries]
        changed = mode==Mode.DECISION and long_quality==88
        assert sent[0].symbol==("ZZZUSDT" if changed else "AAAUSDT")
        assert result.model_dump(mode="json")==before
        intents=store.causal("selection_intent",AT)
        if mode in {Mode.SHADOW,Mode.DECISION}:
            assert len(intents)==1 and intents[0]["delivery_order_changed"]==changed
            assert intents[0]["selection_changed"] is False
            assert not intents[0]["displaced_signal_ids"]
    asyncio.run(scenario())


def test_verified_economic_distribution_and_style_cohorts():
    rows = []
    for i, value in enumerate((Decimal(2), Decimal(-1), Decimal(0))):
        c = candidate(economic_plan_id=f"synthetic:{i}")
        e = evaluate(c, (), mode=Mode.OFF, as_of=AT)
        rows.append(ResearchRow(selection_group=str(i), evaluation=e, trading_style="scalp" if i else "swing",
            outcome=Outcome(economic_plan_id=c.economic_plan_id, verified=True, attribution_ref="synthetic:accounted",
                closed_at=AT+timedelta(hours=1), entry_activated=True, realized_net_r=value,
                fees_r=Decimal("0.01"), slippage_r=Decimal("0.01"))))
    report = compare_policies(tuple(rows))
    result = report["policies"]["CCI_BASELINE"]
    assert result["realized_distribution"]["profit_factor"] == "2"
    assert result["realized_distribution"]["median_net_r"] == "0"
    assert result["verified_fill_rate"] == "1"
    assert set(result["cohorts"]["trading_style"]) == {"scalp", "swing"}
    assert report["policies"]["CMC_DECISION_POLICY"]["retained"]["winners"] == 1
    unverified = tuple(row.model_copy(update={"outcome":row.outcome.model_copy(update={"verified":False})}) for row in rows)
    missing = compare_policies(unverified)["policies"]["CCI_BASELINE"]
    assert missing["realized_distribution"]["median_net_r"] == "N/A"
    assert missing["verified_fill_rate"] == "N/A"
