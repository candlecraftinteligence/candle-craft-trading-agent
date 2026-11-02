"""Exact-settings synthetic approval contracts; process creation is always injected."""
import copy
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.operations.release_activation import digest
from scripts.cci_v2_release_profiles import reviewed_profile, launch_profile
from tests.test_release_profiles import synthetic
from tests.test_release_profiles import exact_approval, APPROVAL_AT
from tests.fixtures.cmc import AT

pytestmark = pytest.mark.no_auto_epoch


def packet_fixture(role="scanner"):
    original, raw, contract = synthetic(role)
    contract["state_root"] = "S:\\SYNTHETIC"
    original["environment_overrides"]["RUNTIME_STATE_ROOT"] = contract["state_root"]
    raw = json.dumps(original).encode()
    contract["profiles"][role]["sha256"] = hashlib.sha256(raw).hexdigest()
    contract["release_trees"] = {"b"*40:"d"*40}
    assets = {"S:\\SYNTHETIC\\identity.json": b'{"bindings":[]}',
        "S:\\SYNTHETIC\\collection.json": b'{"name":"CUSTOM","families":[{"endpoint":"/v1/global-metrics/quotes/latest","seconds":900}]}'}
    after = {"TELEGRAM_SIGNAL_PRESENTATION":"precision", "CMC_MODE":"shadow",
        "CMC_SIDECAR_PATH":"S:\\SYNTHETIC\\cmc_shadow.sqlite",
        "CMC_IDENTITY_MANIFEST":"S:\\SYNTHETIC\\identity.json", "CMC_COLLECTION_PROFILE":"CUSTOM",
        "CMC_COLLECTION_PROFILE_PATH":"S:\\SYNTHETIC\\collection.json",
        "CMC_DAILY_CREDITS":"388", "CMC_MONTHLY_CREDITS":"11988"}
    packet = {"role":role,"release_sha":"b"*40,"candidate_tree":"d"*40,
        "expected_identity":original["expected_identity"], "source_profile_sha256":hashlib.sha256(raw).hexdigest(),
        "contract_sha256":digest(contract),
        "changes":{k:{"before":original["environment_overrides"].get(k),"after":v} for k,v in after.items()},
        "artifacts":{k:{"path":after[k],"sha256":hashlib.sha256(assets[after[k]]).hexdigest()}
            for k in ("CMC_IDENTITY_MANIFEST", "CMC_COLLECTION_PROFILE_PATH")}}
    contract["activation_reviews"] = {role:digest(packet)}
    return raw, contract, packet, assets


def proposal(role="scanner"):
    raw, contract, packet, assets = packet_fixture(role)
    profile = reviewed_profile(raw,role=role,contract=contract,release="b"*40,tree="d"*40,
        activation=packet,read_bytes=assets.__getitem__)
    approval = {"schema":"cci-feature-approval/1", "role":role,"release_sha":"b"*40,"candidate_tree":"d"*40,
        "expected_identity":profile["expected_identity"],"profile_sha256":digest(profile),
        "contract_sha256":digest(contract),"activation_sha256":digest(packet),"changes":packet["changes"],
        "issued_at":AT.isoformat(),"expires_at":(AT+timedelta(hours=1)).isoformat(),"owner_approval_ref":"synthetic:exact-owner-diff"}
    return profile, contract, approval, assets


@pytest.mark.parametrize("role", ["scanner", "listener"])
def test_shadow_precision_launch_only_exact_authorized_settings(role, monkeypatch):
    profile, contract, approval, assets = proposal(role)
    calls = []
    monkeypatch.setenv("CMC_MONTHLY_CREDITS", "999999")
    launched = launch_profile(profile=profile,role=role,contract=contract,approval=approval,processes=[],
        deployed_sha="b"*40,deployed_tree="d"*40,read_bytes=assets.__getitem__,now=AT,
        popen=lambda *a,**k: calls.append((a,k)) or SimpleNamespace(pid=123))
    assert launched.pid == 123 and len(calls) == 1
    env = calls[0][1]["env"]
    assert env["CMC_MODE"] == "shadow" and env["TELEGRAM_SIGNAL_PRESENTATION"] == "precision"
    assert env["CMC_MONTHLY_CREDITS"] == "11988" and env["MARKET_CAP_PROVIDER"] == "coinpaprika"
    assert env["SYNTHETIC_30"] == "value-30"


@pytest.mark.parametrize("fault", ["missing", "expired", "future", "long", "tree", "epoch", "profile", "contract",
    "source", "unknown", "args", "provider", "path", "artifact", "diff", "review", "role"])
def test_malicious_or_stale_launch_never_reaches_process_creation(fault):
    profile, contract, approval, assets = copy.deepcopy(proposal())
    if fault == "missing": approval = {}
    if fault == "expired": approval["expires_at"] = AT.isoformat()
    if fault == "future": approval["issued_at"] = (AT+timedelta(seconds=1)).isoformat()
    if fault == "long": approval["expires_at"] = (AT+timedelta(days=2)).isoformat()
    if fault == "tree": profile["candidate_tree"] = "e"*40
    if fault == "epoch": profile["expected_identity"] = {"epoch_id":"same-but-generation-missing"}
    if fault == "profile": approval["profile_sha256"] = "0"*64
    if fault == "contract": approval["contract_sha256"] = "0"*64
    if fault == "source": profile["_reviewed_source"] += " "
    if fault == "unknown": profile["environment_overrides"]["PYTHONPATH"] = "malicious"
    if fault == "args": profile["scanner_args"] = ["--initialize"]
    if fault == "provider": profile["environment_overrides"]["MARKET_CAP_PROVIDER"] = "cmc"
    if fault == "path": profile["environment_overrides"]["CMC_SIDECAR_PATH"] = "S:\\OTHER\\cmc_shadow.sqlite"
    if fault == "artifact": assets["S:\\SYNTHETIC\\identity.json"] += b" "
    if fault == "diff": approval["changes"] = {}
    if fault == "review": contract["activation_reviews"]["scanner"] = "0"*64
    if fault == "role": approval["role"] = "listener"
    with pytest.raises(ValueError):
        launch_profile(profile=profile,role="scanner",contract=contract,approval=approval,processes=[],
            deployed_sha="b"*40,deployed_tree="d"*40,read_bytes=assets.__getitem__,now=AT,
            popen=lambda *a,**k: pytest.fail("Unsafe process creation"))


@pytest.mark.parametrize("key,value", [("CMC_MODE","decision"),("MARKET_CAP_PROVIDER","cmc"),
    ("CMC_DAILY_CREDITS","389"),("CMC_MONTHLY_CREDITS","12000"),("CMC_API_KEY","secret"),
    ("CMC_SIDECAR_PATH","S:\\SYNTHETIC\\..\\cmc_escape.sqlite")])
def test_even_reviewed_packets_cannot_expand_activation_boundary(key,value):
    raw, contract, packet, assets = packet_fixture()
    packet["changes"][key] = {"before":json.loads(raw)["environment_overrides"].get(key),"after":value}
    contract["activation_reviews"]["scanner"] = digest(packet)
    with pytest.raises(ValueError):
        reviewed_profile(raw,role="scanner",contract=contract,release="b"*40,tree="d"*40,
            activation=packet,read_bytes=assets.__getitem__)


def test_new_exact_source_environment_can_exceed_36_overrides_and_disable_cmc_without_code_rollback():
    active, _, _, _ = proposal()
    source = {k:v for k,v in active.items() if k not in {"_activation","_reviewed_source"}}
    source["release_sha"] = "a"*40
    raw = json.dumps(source).encode()
    _, _, contract = synthetic()
    contract["state_root"] = "S:\\SYNTHETIC"
    contract["profiles"]["scanner"].update(sha256=hashlib.sha256(raw).hexdigest(),
        environment_overrides=source["environment_overrides"])
    contract["release_trees"] = {"b"*40:"d"*40}
    packet = {"role":"scanner","release_sha":"b"*40,"candidate_tree":"d"*40,
        "expected_identity":source["expected_identity"],"source_profile_sha256":hashlib.sha256(raw).hexdigest(),
        "contract_sha256":digest(contract),"changes":{"CMC_MODE":{"before":"shadow","after":"off"}},"artifacts":{}}
    contract["activation_reviews"] = {"scanner":digest(packet)}
    recovered = reviewed_profile(raw,role="scanner",contract=contract,release="b"*40,tree="d"*40,activation=packet)
    assert len(source["environment_overrides"]) > 36
    assert recovered["environment_overrides"] == {**source["environment_overrides"],"CMC_MODE":"off"}
    assert recovered["environment_overrides"]["TELEGRAM_SIGNAL_PRESENTATION"] == "precision"
    assert recovered["scanner_args"] == source["scanner_args"]


def test_precision_only_activation_does_not_require_cmc_artifacts():
    raw, contract, packet, assets = packet_fixture()
    packet["changes"] = {"TELEGRAM_SIGNAL_PRESENTATION":packet["changes"]["TELEGRAM_SIGNAL_PRESENTATION"]}
    packet["artifacts"] = {}
    contract["activation_reviews"]["scanner"] = digest(packet)
    proposed = reviewed_profile(raw,role="scanner",contract=contract,release="b"*40,tree="d"*40,activation=packet)
    assert proposed["environment_overrides"]["CMC_MODE"] == "off"
    assert proposed["environment_overrides"]["TELEGRAM_SIGNAL_PRESENTATION"] == "precision"


@pytest.mark.parametrize("fault", ["stale", "profile", "legacy_approval"])
def test_cmc_off_recovery_also_requires_fresh_exact_approval(fault):
    _,raw,contract = synthetic()
    profile = reviewed_profile(raw,role="scanner",contract=contract,release="b"*40,tree="d"*40)
    approval = exact_approval(profile,contract)
    if fault == "stale": approval["expires_at"] = APPROVAL_AT.isoformat()
    if fault == "profile": approval["profile_sha256"] = "0"*64
    if fault == "legacy_approval": approval = {"release_sha":"b"*40,"owner_approval_ref":"SYNTHETIC"}
    with pytest.raises(ValueError):
        launch_profile(profile=profile,role="scanner",contract=contract,approval=approval,processes=[],
            deployed_sha="b"*40,deployed_tree="d"*40,now=APPROVAL_AT,
            popen=lambda *a,**k: pytest.fail("Unapproved off-mode process creation"))


def test_custom_contract_cannot_relabel_dev_as_runtime(tmp_path,monkeypatch,capsys):
    from scripts import cci_v2_release_profiles as launcher
    path = tmp_path/"forged-contract.json"
    path.write_text(json.dumps({"runtime_host":"SYNTHETIC-DEV"}))
    monkeypatch.setenv("COMPUTERNAME","SYNTHETIC-DEV")
    monkeypatch.setattr("sys.argv",["release","prepare","scanner","--release","repair",
        "--output",str(tmp_path/"proposal.json"),"--contract",str(path)])
    with pytest.raises(SystemExit): launcher.main()
    assert "Runtime-only" in capsys.readouterr().err
    assert not (tmp_path/"proposal.json").exists()
