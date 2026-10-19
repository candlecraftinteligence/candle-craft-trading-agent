# G. CMC MCP

Status: `connected and exercised`.

Cursor MCP namespace user-cmc was ready on 2026-10-19. tools/list returned 12 direct data tools plus find_skill, execute_skill, and mcp_auth. Official prose that says 12 tools matches the direct data tools; the live list is larger. Two data calls were made: get_global_metrics_latest and get_crypto_quotes_latest for id 1. find_skill and execute_skill were not run. No payment header was sent. Global-metrics values are display strings. last_updated was 19 October 2026 12:00 AM UTC+0, which the tool schema describes as a daily label rather than the as-of time of every intraday field. Displayed BTC dominance was +60.24 percent and ETH dominance +10.77 percent. The schema says the leading plus is a share, not a change. Displayed total market cap was 2.76 T. The BTC quote call returned id 1, rank 1, last_updated_time 2026-10-19T10:13:00.000Z, and market_cap_dominance 60.2528. REST global metrics at envelope 2026-10-19T10:14:38.748Z reported btc_dominance 60.245487070864. Those figures are close and not identical; the timestamps and representations differ, so this is not a defect. Definition prose in the global payload is narrative and is not deterministic evidence. The raw MCP payloads are not committed.

Tool names returned by the live session:

- `get_crypto_info`
- `get_crypto_latest_news`
- `get_crypto_marketcap_technical_analysis`
- `get_crypto_metrics`
- `get_crypto_quotes_latest`
- `get_crypto_technical_analysis`
- `get_global_crypto_derivatives_metrics`
- `get_global_metrics_latest`
- `get_upcoming_macro_events`
- `search_crypto_info`
- `search_cryptos`
- `trending_crypto_narratives`
- `find_skill`
- `execute_skill`
- `mcp_auth`

Narrative tool output is not deterministic evidence and is not a CCI gate input.
