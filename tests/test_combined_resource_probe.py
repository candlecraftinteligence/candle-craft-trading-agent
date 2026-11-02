import asyncio
from datetime import timedelta

import pytest

from app.operations.resource_readiness import assess_host
from scripts.cci_v2_resource_probe import synthetic_probe
from tests.fixtures.cmc import AT

pytestmark = pytest.mark.no_auto_epoch
MIB = 1024*1024


def host(**changes):
    args = dict(observed_at=AT,now=AT,total_physical=3973*MIB,available_physical=571*MIB,
        commit_total=2000*MIB,commit_limit=8000*MIB,scanner_peak=623*MIB,listener_peak=74*MIB,
        cmc_incremental_peak=None,retained_floor=512*MIB)
    return assess_host(**{**args,**changes})


def test_host_measurement_sufficiency_and_physical_commit_are_separate():
    assert host()["status"] == "UNVERIFIED"
    assert host(cmc_incremental_peak=100*MIB)["status"] == "BLOCKED"
    assert host(cmc_incremental_peak=100*MIB,commit_limit=64000*MIB)["status"] == "BLOCKED"
    assert host(cmc_incremental_peak=100*MIB,available_physical=2000*MIB)["status"] == "UNVERIFIED"
    assert host(cmc_incremental_peak=100*MIB,available_physical=2000*MIB,
        simultaneous_peak_verified=True)["status"] == "VERIFIED"
    assert host(cmc_incremental_peak=100*MIB,now=AT+timedelta(minutes=6))["status"] == "UNVERIFIED"
    assert host(cmc_incremental_peak=100*MIB,available_physical=2000*MIB,commit_limit=2000*MIB)["status"] == "BLOCKED"


def test_bounded_four_page_collector_and_sidecar_memory_probe():
    result = asyncio.run(synthetic_probe(pair_count=820,padding=400))
    assert result["real_network_calls"] == 0 and result["restart_additional_http_calls"] == 0
    assert result["pages"] == 4 and result["eligible_synthetic_symbols"] == 55
    assert result["max_http_body_bytes"] <= 1024*1024
    assert result["largest_record_bytes"] <= 2*1024*1024
    assert result["sqlite_file_bytes"] <= 64*1024*1024
    assert result["tracemalloc_peak_bytes"] > 0 and result["peak_rss_bytes"] > 0
    assert result["runtime_resource_readiness"] == "UNVERIFIED"
