# market-data-intelligence-foundation

Research-only market-data evidence phase. Production gates, schema, and providers are unchanged.

Base SHA: `962dbbb35db942beef0423c0763555e665da668d`
Head SHA: the pull request head. Opening commit `579fb7966726a8a78809c3c190d49330a9ef27d0` predates this packet update. Review the final PR head.
PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/136

## Index

- [Plan and safety](00_plan.md)
- [A. Inventory](A_inventory.md)
- [B. Dependency map](B_dependency_map.md)
- [C. Candles and volume](C_candle_volume.md)
- [D. OI, funding, positioning](D_oi_funding_positioning.md)
- [E. Freshness and missingness](E_freshness_missingness.md)
- [F. Provider matrix](F_provider_matrix.md)
- [G. CMC MCP](G_cmc_mcp.md)
- [H. Top-100 shadow](H_top100.md)
- [I. Shadow comparison](I_shadow.md)
- [J. Cadence and cost](J_cadence_cost.md)
- [K. Recommendations](K_recommendations.md)
- [L. Review packet](L_review_packet.md)
- [Evidence contract](evidence_contract.md)

Raw provider payloads, when a live round succeeds, stay under gitignored `research_runs/market_data_foundation/`.
A hash in this package does not by itself let a reviewer rebuild a payload that was not committed.

Runtime enablement for every production source is `unverified`. Code defaults are not deployment evidence.
