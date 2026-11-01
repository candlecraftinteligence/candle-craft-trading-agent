"""Guarded release launch-profile preparation. Never initializes or opens SQLite.

Full environment values are read only from the hash-bound active Runtime profile.
DEV tests inject synthetic profiles/processes; the CLI refuses execution on DEV.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT/"config/releases/runtime-profile-contract.json"


def reviewed_profile(raw: bytes, *, role: str, contract: dict, release: str, tree: str) -> dict:
    binding = contract["profiles"][role]
    if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise ValueError("Active profile hash differs from reviewed Runtime preflight")
    profile = json.loads(raw)
    if profile["release_sha"] != contract["baseline"] or profile["expected_identity"] != contract["expected_identity"]:
        raise ValueError("Baseline release or complete epoch identity differs")
    args_key = "scanner_args" if role == "scanner" else "listener_args"
    if profile[args_key] != binding["arguments"]:
        raise ValueError("Argument vector differs from verified profile")
    overrides = profile["environment_overrides"]
    required = {"LOCAL_MANUAL_MODE": "true", "ORDER_EXECUTION_ENABLED": "false",
                "CMC_MODE": "off", "MARKET_CAP_PROVIDER": "coinpaprika",
                "RUNTIME_STATE_ROOT": contract["state_root"]}
    if len(overrides) != 36 or any(str(overrides.get(k)).lower() != v.lower() for k,v in required.items()):
        raise ValueError("Complete environment or safety binding differs")
    if release not in (contract["implementation"], contract["rollback"]) or len(tree) != 40:
        raise ValueError("Unreviewed release or missing immutable tree")
    result = copy.deepcopy(profile)
    result.update(release_sha=release, candidate_tree=tree, previous_approved_release=contract["baseline"],
                  owner_approved_release=None, approval_state="PROPOSED_NOT_AUTHORIZED", ci=None)
    return result


def launch_profile(*, profile: dict, role: str, contract: dict, approval: dict, processes: list[dict],
                   deployed_sha: str, deployed_tree: str, popen=subprocess.Popen):
    if approval.get("release_sha") != deployed_sha or not approval.get("owner_approval_ref"):
        raise ValueError("Missing separate exact-release owner approval")
    if profile["release_sha"] != deployed_sha or profile["candidate_tree"] != deployed_tree:
        raise ValueError("Deployed code identity differs")
    if profile["expected_identity"] != contract["expected_identity"]:
        raise ValueError("Complete epoch identity differs")
    needle = "run_scan.py" if role == "scanner" else "run_telegram_bot.py"
    if any(needle in str(p.get("CommandLine", "")).lower() for p in processes):
        raise ValueError("Existing owner or launcher still present; do not compete")
    args = profile["scanner_args" if role == "scanner" else "listener_args"]
    if args != contract["profiles"][role]["arguments"]:
        raise ValueError("Launch arguments changed")
    env = {**os.environ, **{k:str(v) for k,v in profile["environment_overrides"].items()}}
    for key, value in (("CMC_MODE","off"),("MARKET_CAP_PROVIDER","coinpaprika"),
                       ("LOCAL_MANUAL_MODE","true"),("ORDER_EXECUTION_ENABLED","false")):
        if env.get(key, "").lower() != value:
            raise ValueError("Safety or separate CMC activation boundary differs")
    startup = subprocess.STARTUPINFO() if os.name == "nt" else None
    if startup is not None:
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
    repo = Path(contract["repository"])
    return popen([str(repo/".venv/Scripts/python.exe"), "-u", str(repo/"scripts"/needle), *args],
                 cwd=repo, env=env, creationflags=getattr(subprocess,"CREATE_NEW_CONSOLE",0), startupinfo=startup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start"))
    parser.add_argument("role", choices=("scanner", "listener"))
    parser.add_argument("--release", choices=("repair", "rollback"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="New proposed profile; never overwrite an active profile")
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    contract = json.loads(CONTRACT.read_text())
    if os.environ.get("COMPUTERNAME") != contract["runtime_host"]:
        parser.error("Runtime-only operation; DEV must use injected synthetic tests")
    if not (Path(contract["state_root"])/"candle_craft.db").is_file():
        parser.error("Existing authoritative database missing; never initialize a replacement")
    release = contract["implementation" if args.release == "repair" else "rollback"]
    def git(*cmd):
        return subprocess.check_output(["git", *cmd], cwd=contract["repository"], text=True, timeout=10).strip()
    tree = git("rev-parse", f"{release}^{{tree}}")
    raw = Path(contract["profiles"][args.role]["path"]).read_bytes()
    proposal = reviewed_profile(raw, role=args.role, contract=contract, release=release, tree=tree)
    if args.action == "prepare":
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(proposal,file,indent=2)
        print("Proposed profile prepared from exact reviewed active profile; no approval or activation granted")
        return
    if args.approval is None:
        parser.error("Start requires separately approved exact-release plan")
    # Recompute the proposal from the trusted source; never execute an arbitrary output file.
    if json.loads(args.output.read_text()) != proposal:
        parser.error("Proposed profile differs from reviewed source derivation")
    inventory = subprocess.check_output(["powershell.exe","-NoProfile","-NonInteractive","-Command",
        "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"], text=True, timeout=15)
    processes = json.loads(inventory or "[]")
    if isinstance(processes,dict):
        processes = [processes]
    child = launch_profile(profile=proposal, role=args.role, contract=contract,
        approval=json.loads(args.approval.read_text()), processes=processes,
        deployed_sha=git("rev-parse","HEAD"), deployed_tree=git("rev-parse","HEAD^{tree}"))
    print(json.dumps({"launcher_pid":child.pid,"release":release,"role":args.role,
                      "owner_receipt_verification":"Required before acceptance"}))


if __name__ == "__main__":
    main()
