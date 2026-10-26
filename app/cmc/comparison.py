"""Compare only equivalent, causal observations. Aggregate OI is never contract OI."""
from datetime import datetime
from decimal import Decimal

from app.cmc.models import Comparison as C, EvidenceState as S, Observation


def compare(left: Observation, right: Observation, *, as_of: datetime,
            tolerance: Decimal = Decimal("0.02"), max_skew_seconds: int = 30) -> C:
    if left.status_at(as_of) != S.VERIFIED or right.status_at(as_of) != S.VERIFIED:
        return C.UNVERIFIED
    if left.scope != "contract" or right.scope != "contract" or left.metric != right.metric:
        return C.NOT_COMPARABLE
    if left.instrument is None or right.instrument is None or left.instrument.key != right.instrument.key:
        return C.IDENTITY_MISMATCH
    if (left.units != right.units or left.funding_interval_seconds != right.funding_interval_seconds
            or left.rate_semantics != right.rate_semantics or left.counting_convention != right.counting_convention):
        return C.UNIT_MISMATCH
    if abs((left.source_at - right.source_at).total_seconds()) > max_skew_seconds:
        return C.TEMPORALLY_MISALIGNED
    if left.value is None or right.value is None:
        return C.UNVERIFIED
    scale = max(abs(left.value), abs(right.value), Decimal("0.00000001"))
    return C.CONSISTENT if abs(left.value-right.value) / scale <= tolerance else C.MATERIAL_DIVERGENCE
