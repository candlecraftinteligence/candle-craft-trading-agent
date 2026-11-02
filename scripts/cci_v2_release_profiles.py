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
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.operations.release_activation import activate, validate_approval, immutable_sha
CONTRACT = ROOT/"config/releases/runtime-profile-contract.json"


def reviewed_profile(raw: bytes, *, role: str, contract: dict, release: str, tree: str,
                     activation: dict | None = None, read_bytes=lambda p: Path(p).read_bytes()) -> dict:
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
                "MARKET_CAP_PROVIDER": "coinpaprika",
                "RUNTIME_STATE_ROOT": contract["state_root"]}
    reviewed_environment = binding.get("environment_overrides")
    source_valid = (overrides == reviewed_environment if reviewed_environment is not None else
                    len(overrides) == 36 and overrides.get("CMC_MODE") == "off")
    if (not source_valid or overrides.get("CMC_MODE") not in {"off", "shadow"}
            or any(str(overrides.get(k)).lower() != v.lower() for k,v in required.items())):
        raise ValueError("Complete environment or safety binding differs")
    if release not in (contract["implementation"], contract["rollback"]) or not immutable_sha(tree) or not immutable_sha(release):
        raise ValueError("Unreviewed release or missing immutable tree")
    result = copy.deepcopy(profile)
    result.update(release_sha=release, candidate_tree=tree, previous_approved_release=contract["baseline"],
                  owner_approved_release=None, approval_state="PROPOSED_NOT_AUTHORIZED", ci=None)
    result["_reviewed_source"] = raw.decode("utf-8")
    result["_activation"] = activation
    if activation is not None:
        result["environment_overrides"] = activate(overrides, activation, contract=contract, role=role,
            release=release, tree=tree, source_hash=binding["sha256"], read_bytes=read_bytes)
    return result


def launch_profile(*, profile: dict, role: str, contract: dict, approval: dict, processes: list[dict],
                   deployed_sha: str, deployed_tree: str, popen=subprocess.Popen,
                   read_bytes=lambda p: Path(p).read_bytes(), now=None):
    if approval.get("release_sha") != deployed_sha or not approval.get("owner_approval_ref"):
        raise ValueError("Missing separate exact-release owner approval")
    if profile["release_sha"] != deployed_sha or profile["candidate_tree"] != deployed_tree:
        raise ValueError("Deployed code identity differs")
    if profile["expected_identity"] != contract["expected_identity"]:
        raise ValueError("Complete epoch identity differs")
    derived = reviewed_profile(profile["_reviewed_source"].encode("utf-8"), role=role,
        contract=contract, release=deployed_sha, tree=deployed_tree,
        activation=profile.get("_activation"), read_bytes=read_bytes)
    if profile != derived:
        raise ValueError("Proposed profile differs from exact reviewed derivation")
    validate_approval(approval, profile=profile, role=role, contract=contract, now=now)
    if deployed_sha == contract["rollback"]:
        from app.operations.rollback_compatibility import require_rollback_proof
        require_rollback_proof(contract, deployed_sha, deployed_tree)
    needle = "run_scan.py" if role == "scanner" else "run_telegram_bot.py"
    if any(needle in str(p.get("CommandLine", "")).lower() for p in processes):
        raise ValueError("Existing owner or launcher still present; do not compete")
    args = profile["scanner_args" if role == "scanner" else "listener_args"]
    if args != contract["profiles"][role]["arguments"]:
        raise ValueError("Launch arguments changed")
    # Feature settings cannot leak in from the launcher's unreviewed environment.
    from app.operations.release_activation import FEATURE_KEYS
    env = {**{k:v for k,v in os.environ.items() if k.upper() not in FEATURE_KEYS},
           **{k:str(v) for k,v in profile["environment_overrides"].items()}}
    for key, value in (("MARKET_CAP_PROVIDER","coinpaprika"),
                       ("LOCAL_MANUAL_MODE","true"),("ORDER_EXECUTION_ENABLED","false")):
        if env.get(key, "").lower() != value:
            raise ValueError("Safety or separate CMC activation boundary differs")
    if env.get("CMC_MODE") not in ({"off", "shadow"} if profile.get("_activation") else {"off"}):
        raise ValueError("CMC activation boundary differs")
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
    parser.add_argument("--contract", type=Path, default=CONTRACT, help="Exact newly staged release contract")
    parser.add_argument("--activation", type=Path, help="Reviewed exact feature differences; does not grant approval")
    args = parser.parse_args()
    boundary = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if os.environ.get("COMPUTERNAME") != boundary["runtime_host"]:
        parser.error("Runtime-only operation; DEV must use injected synthetic tests")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    if (any(contract.get(k) != boundary[k] for k in ("runtime_host", "repository", "state_root", "expected_identity"))
            or any(contract["profiles"][role]["arguments"] != boundary["profiles"][role]["arguments"]
                for role in ("scanner", "listener"))):
        parser.error("Staged contract changed protected host/path/epoch/argument boundaries")
    if not (Path(contract["state_root"])/"candle_craft.db").is_file():
        parser.error("Existing authoritative database missing; never initialize a replacement")
    release = contract["implementation" if args.release == "repair" else "rollback"]
    def git(*cmd):
        return subprocess.check_output(["git", *cmd], cwd=contract["repository"], text=True, timeout=10).strip()
    tree = git("rev-parse", f"{release}^{{tree}}")
    raw = Path(contract["profiles"][args.role]["path"]).read_bytes()
    activation = json.loads(args.activation.read_text(encoding="utf-8")) if args.activation else None
    proposal = reviewed_profile(raw, role=args.role, contract=contract, release=release, tree=tree, activation=activation)
    if args.action == "prepare":
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(proposal,file,indent=2)
        print("Proposed profile prepared from exact reviewed active profile; no approval or activation granted")
        return
    if args.approval is None:
        parser.error("Start requires separately approved exact-release plan")
    if args.release == "rollback":
        from app.operations.rollback_compatibility import require_rollback_proof
        require_rollback_proof(contract, release, tree)
    # Recompute the proposal from the trusted source; never execute an arbitrary output file.
    if json.loads(args.output.read_text()) != proposal:
        parser.error("Proposed profile differs from reviewed source derivation")
    # All launches require fresh exact approval, including CMC-off recovery.
    validate_approval(json.loads(args.approval.read_text(encoding="utf-8")),
        profile=proposal, role=args.role, contract=contract)
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
