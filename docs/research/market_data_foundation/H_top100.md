# H. Global Top-100 shadow

The boundary is ranks 1 through 100 before exclusions. Rank 101 is never used to refill a stable, wrapped, ambiguous, or unsupported asset.

Existing policy excludes stablecoin bases and leveraged-token bases at the Binance contract filter. Wrapped assets are not merged into an underlying. A `1000` prefix is not stripped. Symbol collisions are excluded. Missing quote volume is insufficient evidence, not zero.

Owner monitoring is a separate count. This phase does not execute the production scanner, so scan attempts are zero and are not called successful scans.

Synthetic stage tests cover the boundary. A live stage count below is included only when an online summary is attached, and its `count_kind` says whether it is live or a synthetic transport.

```json
{
  "count_kind": "live",
  "requested_global_assets": 100,
  "received_rows": 100,
  "valid_ranked_assets": 100,
  "identity_validated_assets": 100,
  "supported_eligible_contracts": 74,
  "liquidity_qualified_contracts": 74,
  "unique_queued_discovery_contracts": 74,
  "scan_attempts": 0,
  "successfully_completed_scans": 0,
  "insufficient_evidence_rejections": 0,
  "other_failures": 26,
  "accepted_outcomes": 0,
  "rejected_outcomes": 0,
  "owner_monitoring_count": "not queried from Runtime; synthetic test covers owner continuity",
  "membership_status": "verified",
  "membership_problems": [],
  "exclusion_reason_counts": {
    "existing_stable_or_leveraged_policy": 5,
    "no_supported_usdt_perpetual": 21
  },
  "liquidity_minimum_usdt": "0, the existing market-cap default",
  "sample_symbols": [
    "BTCUSDT",
    "ETHUSDT",
    "ZECUSDT",
    "STRKUSDT",
    "VVVUSDT",
    "ASTERUSDT",
    "PENDLEUSDT",
    "BSVUSDT"
  ],
  "sample_rule": "btc_eth_plus_volume_halves_spread_tiebreak_symbol",
  "coinpaprika": {
    "status": "methodology_comparison",
    "cmc_symbols_in_ranks_1_to_100": 100,
    "coinpaprika_unique_symbols_in_ranks_1_to_100": 99,
    "symbol_overlap": 71,
    "rank_disagreements_among_overlap": 62,
    "defect_claimed": false,
    "parser_note": "duplicate CoinPaprika symbols keep the first row, so 99 unique symbols is not proof the payload had only 99 ranked assets"
  },
  "cmc_global_rest": {
    "btc_dominance": "60.245487070864",
    "eth_dominance": "10.773376674445",
    "total_market_cap_usd": "2758181354641.7075",
    "last_updated": "2026-10-19T10:12:59.999Z",
    "envelope_timestamp": "2026-10-19T10:14:38.748Z",
    "credit_count": "1"
  },
  "response_sha256": {
    "cmc_listings": "5fa6dcd6af9399a29a071f92c91c49b8c5d694bf62a2c956b209f14eb739c8bf",
    "cmc_global": "cf3e78085d2207ac88468461539b35dcbdd4033ea248e335b5a1ebc19a2eb21f"
  },
  "raw_bodies_committed": false
}
```
