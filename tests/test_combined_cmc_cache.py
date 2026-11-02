import copy
import sqlite3
import hashlib
import json
from decimal import Decimal
from datetime import timedelta

import pytest

from app.cmc.models import encoded
from app.cmc.restart_cache import KIND, save_family, restore_family
from app.cmc.client import Fetch
from app.cmc.endpoints import Endpoint
from app.cmc.models import EvidenceState
from app.cmc.storage import Sidecar
from tests.fixtures.cmc import AT
from tests.test_combined_cmc_acquisition import pages_fixture

pytestmark = pytest.mark.no_auto_epoch


def large_family():
    _, _, pages = pages_fixture()
    # Each source page is bounded; the four-page serialized family exceeds 2 MiB.
    for f in pages:
        f.data["padding"] = "SYNTHETIC"*32000
    return pages


def test_oversized_family_partitioning_restart_original_timestamps_and_idempotency(tmp_path):
    store = Sidecar(tmp_path/"cmc_fragmented.sqlite")
    pages = large_family()
    assert len(encoded([f.model_dump(mode="json") for f in pages]).encode()) > 2*1024*1024
    save_family(store,"synthetic:family",pages)
    with store.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        assert db.execute("SELECT MAX(length(CAST(payload AS BLOB))) FROM evidence").fetchone()[0] <= 2*1024*1024
    save_family(store,"synthetic:family",pages)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == count
    restarted = Sidecar(store.path)
    manifest = restarted.causal(KIND,AT+timedelta(seconds=30),limit=1)[0]
    recovered = restore_family(restarted,manifest,AT+timedelta(seconds=30))
    assert recovered == tuple(pages) and all(p.acquired_at == AT for p in recovered)
    assert store.path.stat().st_size <= store.max_bytes


@pytest.mark.parametrize("fault", ["partial", "evicted", "duplicate", "timestamp", "hash", "retention"])
def test_partial_corrupt_or_stale_family_never_restores(tmp_path,fault):
    store = Sidecar(tmp_path/"cmc_incomplete.sqlite")
    save_family(store,"synthetic:family",large_family())
    manifest = store.causal(KIND,AT,limit=1)[0]
    if fault in {"partial", "evicted"}:
        with store.connect() as db:
            db.execute("DELETE FROM evidence WHERE id=?",(manifest["pages"][-1]["fragments"][-1],))
    if fault == "duplicate": manifest["pages"][1]["fragments"] = manifest["pages"][0]["fragments"]
    if fault == "timestamp": manifest["pages"][0]["acquired_at"] = (AT+timedelta(seconds=1)).isoformat()
    if fault == "hash": manifest["pages"][0]["sha256"] = "0"*64
    as_of = AT+timedelta(days=31) if fault == "retention" else AT
    with pytest.raises(ValueError): restore_family(Sidecar(store.path),manifest,as_of)


def test_small_file_and_record_caps_remain_fail_closed(tmp_path):
    store = Sidecar(tmp_path/"cmc_small.sqlite",max_bytes=65536,max_records=3)
    with pytest.raises((ValueError,sqlite3.Error)):
        save_family(store,"synthetic:family",large_family())
    assert store.path.stat().st_size <= 65536


def test_original_provider_decimal_types_and_stale_source_time_survive_restart(tmp_path):
    store = Sidecar(tmp_path/"cmc_decimal.sqlite")
    text = '{"status":{"error_code":0},"data":{"price":100.125,"source_at":"2025-12-31T00:00:00Z"}}'
    raw = json.loads(text,parse_float=Decimal)
    fetch = Fetch(endpoint=Endpoint.GLOBAL,acquired_at=AT,state=EvidenceState.UNVERIFIED,
        data=raw["data"],raw=raw,raw_text=text,raw_ref=hashlib.sha256(text.encode()).hexdigest())
    save_family(store,"synthetic:decimal",[fetch])
    manifest = store.causal(KIND,AT,limit=1)[0]
    recovered = restore_family(Sidecar(store.path),manifest,AT+timedelta(minutes=1))[0]
    assert recovered == fetch and isinstance(recovered.data["price"],Decimal)
    assert recovered.data["source_at"] == "2025-12-31T00:00:00Z" and recovered.acquired_at == AT
    assert store.usage(AT) == {}


def test_interrupted_fragment_write_never_publishes_a_family(tmp_path,monkeypatch):
    store = Sidecar(tmp_path/"cmc_partial_write.sqlite")
    original = store.put
    writes = 0
    def interrupted(kind,*args,**kwargs):
        nonlocal writes
        writes += 1
        if writes == 3: raise OSError("synthetic interrupted write")
        return original(kind,*args,**kwargs)
    monkeypatch.setattr(store,"put",interrupted)
    with pytest.raises(OSError): save_family(store,"synthetic:interrupted",large_family())
    assert Sidecar(store.path).causal(KIND,AT,limit=1) == []
