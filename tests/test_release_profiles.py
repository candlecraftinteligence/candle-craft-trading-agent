"""Synthetic launch profiles; no process or production state is accessed."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
from types import SimpleNamespace

import pytest

from scripts.cci_v2_release_profiles import reviewed_profile, launch_profile

pytestmark = pytest.mark.no_auto_epoch


def synthetic(role="scanner"):
    profile = {"release_sha":"a"*40, "expected_identity":{"epoch_id":"SYNTHETIC", "generation_binding":"unchanged"},
        "environment_overrides":{"LOCAL_MANUAL_MODE":"true","ORDER_EXECUTION_ENABLED":"false","CMC_MODE":"off",
            "MARKET_CAP_PROVIDER":"coinpaprika", "RUNTIME_STATE_ROOT":"SYNTHETIC-STATE",
            **{f"SYNTHETIC_{n}":f"value-{n}" for n in range(31)}},
        "scanner_args":["--database-path","SYNTHETIC.db"], "listener_args":["--state-path","unchanged.json"],
        "approval_state":"OLD_APPROVAL", "owner_approved_release":"a"*40, "ci":"old-ci"}
    raw=json.dumps(profile).encode()
    contract={"baseline":"a"*40,"implementation":"b"*40,"rollback":"c"*40,"state_root":"SYNTHETIC-STATE",
        "expected_identity":profile["expected_identity"],"repository":"SYNTHETIC-REPO",
        "profiles":{role:{"sha256":hashlib.sha256(raw).hexdigest(),"arguments":profile["scanner_args" if role=="scanner" else "listener_args"]}}}
    return profile,raw,contract


@pytest.mark.parametrize("role",["scanner","listener"])
@pytest.mark.parametrize("release",["b"*40,"c"*40])
def test_exact_profile_derivation_preserves_every_override_argument_and_epoch(role,release):
    profile,raw,contract=synthetic(role)
    proposed=reviewed_profile(raw,role=role,contract=contract,release=release,tree="d"*40)
    assert proposed["environment_overrides"]==profile["environment_overrides"]
    assert proposed["expected_identity"]==profile["expected_identity"]
    assert proposed["scanner_args"]==profile["scanner_args"] and proposed["listener_args"]==profile["listener_args"]
    assert proposed["owner_approved_release"] is None and proposed["ci"] is None
    calls=[]
    def fake(*args,**kwargs):
        calls.append((args,kwargs))
        return SimpleNamespace(pid=123)
    launch_profile(profile=proposed,role=role,contract=contract,
        approval={"release_sha":release,"owner_approval_ref":"SYNTHETIC-ONLY"},processes=[],
        deployed_sha=release,deployed_tree="d"*40,popen=fake)
    assert len(calls)==1
    args,options=calls[0]
    assert options["env"]["CMC_MODE"]=="off" and options["env"]["SYNTHETIC_30"]=="value-30"
    assert list(args[0][-2:])==contract["profiles"][role]["arguments"]
    if options["startupinfo"] is not None:
        assert options["creationflags"] and options["startupinfo"].wShowWindow==0


@pytest.mark.parametrize("fault",["hash","epoch","arguments","environment","funding-release"])
def test_changed_active_profile_is_rejected(fault):
    profile,raw,contract=synthetic()
    if fault=="hash": raw+=b" "
    else:
        if fault=="epoch":profile["expected_identity"]={"epoch_id":"OTHER"}
        if fault=="arguments":profile["scanner_args"]=["--initialize-new-epoch"]
        if fault=="environment":profile["environment_overrides"]["CMC_MODE"]="shadow"
        if fault=="funding-release":profile["release_sha"]="e"*40
        raw=json.dumps(profile).encode()
        contract["profiles"]["scanner"]["sha256"]=hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):reviewed_profile(raw,role="scanner",contract=contract,release="b"*40,tree="d"*40)


@pytest.mark.parametrize("fault",["owner","approval","epoch","tree","code","cmc","args"])
def test_start_refuses_unsafe_or_competing_owner_before_popen(fault):
    profile,raw,contract=synthetic()
    proposed=reviewed_profile(raw,role="scanner",contract=contract,release="b"*40,tree="d"*40)
    approval={"release_sha":"b"*40,"owner_approval_ref":"SYNTHETIC-ONLY"}
    processes=[]
    if fault=="owner":processes=[{"CommandLine":"python scripts/run_scan.py --watch"}]
    if fault=="approval":approval={}
    if fault=="epoch":proposed["expected_identity"]={}
    if fault=="tree":proposed["candidate_tree"]="e"*40
    if fault=="code":proposed["release_sha"]="e"*40
    if fault=="cmc":proposed["environment_overrides"]["CMC_MODE"]="shadow"
    if fault=="args":proposed["scanner_args"]=["--new-state"]
    def forbidden(*a,**k):pytest.fail("Unsafe launch reached process creation")
    with pytest.raises(ValueError):
        launch_profile(profile=proposed,role="scanner",contract=contract,approval=approval,processes=processes,
                       deployed_sha="b"*40,deployed_tree="d"*40,popen=forbidden)


@pytest.mark.skipif(os.name != "nt",reason="Windows PowerShell parser and DEV host guard")
def test_powershell_controls_parse():
    folder=Path(__file__).resolve().parents[1]/"runtime_audits/launcher"
    for script in folder.glob("*.ps1"):
        literal=str(script).replace("'","''")
        command=f"$TokenList=$null; $ErrorList=$null; [System.Management.Automation.Language.Parser]::ParseFile('{literal}',[ref]$TokenList,[ref]$ErrorList) | Out-Null; if ($ErrorList.Count) {{ $ErrorList; exit 1 }}"
        result=subprocess.run([shutil.which("pwsh") or "powershell.exe","-NoProfile","-NonInteractive","-Command",command],capture_output=True,text=True)
        assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.skipif(os.name != "nt",reason="Windows PowerShell DEV process-control refusal")
@pytest.mark.parametrize("name,args",[
    ("start_runtime.ps1",["-Role","scanner","-Release","repair","-ProposedProfile","SYNTHETIC","-ApprovedPlan","SYNTHETIC","-PacketRoot","SYNTHETIC"]),
    ("stop_runtime.ps1",["-Role","scanner","-WorkerPid","1","-LauncherPid","2","-WorkerCreatedUtc","2000-01-01T00:00:00Z","-LauncherCreatedUtc","2000-01-01T00:00:00Z","-OwnerApprovalRef","SYNTHETIC"]),
    ("rollback_runtime.ps1",["-Role","scanner","-VerifiedRollbackProfile","SYNTHETIC","-ApprovedPlan","SYNTHETIC","-PacketRoot","SYNTHETIC"]),
])
def test_powershell_refuses_dev_before_process_or_state_access(name,args):
    script=Path(__file__).resolve().parents[1]/"runtime_audits/launcher"/name
    result=subprocess.run([shutil.which("pwsh") or "powershell.exe","-NoProfile","-NonInteractive","-File",str(script),*args],
        env={**os.environ,"COMPUTERNAME":"SYNTHETIC-DEV"},capture_output=True,text=True)
    assert result.returncode != 0 and "Runtime-only" in result.stdout+result.stderr
