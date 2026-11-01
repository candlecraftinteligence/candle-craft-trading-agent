"""Exact Git sources share a populated synthetic DB across independent processes."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

pytestmark = pytest.mark.no_auto_epoch
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT/"config/releases/cci-v2-compatible-rollback.json").read_text())


def test_exact_baseline_repair_rollback_preserve_state_and_recover_only_unsent_parts(tmp_path):
    stages = [("baseline", MANIFEST["baseline"]),
              ("repair", "677181fab8a6bbff316b0d0a0c551cc82d1faab1"),
              ("rollback", MANIFEST["rollback_commit"]),
              ("restart", MANIFEST["rollback_commit"])]
    patch = subprocess.check_output(["git", "diff", "--numstat", stages[0][1], stages[2][1]], cwd=ROOT).decode().splitlines()
    assert patch == ["1\t0\tapp/context/models.py"]
    receipts = []
    for phase, commit in stages:
        target = tmp_path/phase
        archive = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=ROOT)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            z.extractall(target)
        result = subprocess.run([sys.executable, str(ROOT/"tests/fixtures/release_roundtrip_probe.py"),
                                 str(target), str(tmp_path/"state"), phase], cwd=target,
            env={**os.environ, "PYTHONPATH": str(target), "CMC_MODE":"off", "LOCAL_MANUAL_MODE":"true",
                 "ORDER_EXECUTION_ENABLED":"false", "TELEGRAM_DRY_RUN":"true", "TELEGRAM_SIGNALS_ENABLED":"false",
                 "PYTHONIOENCODING":"utf-8"}, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert result.returncode == 0, result.stdout+result.stderr
        receipts.append(json.loads(result.stdout.splitlines()[-1]))
    assert [r["mixed_payload_reads"] for r in receipts] == [2,4,4,4]
    assert all(r["epoch_preserved"] and r["command_offset"]==700 and r["real_http_calls"]==0 for r in receipts)
