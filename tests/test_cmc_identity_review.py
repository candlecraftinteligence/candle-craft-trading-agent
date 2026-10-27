from datetime import timedelta
from pathlib import Path

import pytest

from app.cmc.collection import IdentityManifest, load_manifest
from app.cmc.identity import AssetBinding, IdentityStatus, binding_status
from app.cmc.identity_review import propose_identities
from app.cmc.models import EvidenceState as S, utc
from test_cmc_evidence import _mapping
from tests.fixtures.cmc import AT, pair

pytestmark = pytest.mark.no_auto_epoch
REAL = Path(__file__).parent / "fixtures/cmc_authenticated"


def approved(**changes):
    data = dict(cmc_id=1, cmc_slug="bitcoin", binance_base="BTC", contract_symbol="BTCUSDT",
        exchange_id=270, market_id=100, acquired_at=AT-timedelta(days=2),
        reviewed_at=AT-timedelta(days=1), approved_at=AT-timedelta(hours=1),
        expires_at=AT+timedelta(days=1), approved=True, owner_approval_ref="synthetic:owner",
        evidence_ref="synthetic:independent-identities")
    return AssetBinding(**{**data, **changes})


def test_historical_proposal_has_exact_ids_and_no_owner_approvals(tmp_path):
    at = utc("2026-10-27T00:00:00Z")
    manifest, evidence = propose_identities(REAL, as_of=at)
    assert {(b.cmc_id, b.cmc_slug, b.contract_symbol, b.exchange_id, b.market_id) for b in manifest.bindings} == {
        (1,"bitcoin","BTCUSDT",270,47150), (1027,"ethereum","ETHUSDT",270,47205),
        (5426,"solana","SOLUSDT",270,67944)}
    assert all(not b.approved and b.owner_approval_ref is None and b.reviewed_at is None
        and b.approved_at is None for b in manifest.bindings)
    assert all(row["historical_evidence_status"] == "EVIDENCE_VERIFIED" and
        row["authorization_status"] == "IDENTITY_REVIEW_PENDING" for row in evidence)
    path = tmp_path / "review.json"
    path.write_text(manifest.model_dump_json())
    assert load_manifest(path) == manifest
    for b in manifest.bindings:
        assert binding_status(b, at+timedelta(days=10)) == IdentityStatus.EXPIRED


@pytest.mark.parametrize("changes", [
    {"approved":False}, {"owner_approval_ref":None}, {"owner_approval_ref":"PENDING"},
    {"market_id":101}, {"exchange_id":521}, {"contract_symbol":"BTCUSDC"}, {"quote_id":999},
    {"expires_at":AT}, {"acquired_at":AT+timedelta(seconds=1)},
    {"reviewed_at":AT+timedelta(seconds=1)}, {"approved_at":AT+timedelta(seconds=1)},
    {"reviewed_at":AT.replace(tzinfo=None)}, {"reviewed_at":None}, {"market_id":None}, {"evidence_ref":""},
])
def test_production_mapping_requires_exact_current_owner_review(changes):
    result = _mapping(binding=approved(**changes), require_owner_approval=True)
    assert result.state != S.VERIFIED and result.instrument is None


def test_wrong_source_market_is_rejected_and_approval_expires():
    assert _mapping(binding=approved(), require_owner_approval=True).state == S.VERIFIED
    wrong = {**pair(), "market_id":999}
    assert _mapping(binding=approved(), pair=wrong, require_owner_approval=True).state != S.VERIFIED
    assert _mapping(binding=approved(), metadata={}).instrument is None


def test_conflicting_bindings_rejected_even_without_file_loader():
    with pytest.raises(ValueError):
        IdentityManifest(bindings=(approved(), approved(cmc_id=2)))
    with pytest.raises(ValueError):
        IdentityManifest(bindings=(approved(), approved(binance_base="OTHER", contract_symbol="OTHERUSDT")))
