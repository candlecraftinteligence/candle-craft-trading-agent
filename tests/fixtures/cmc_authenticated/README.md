# Real authenticated validation capture

These are selected public response rows acquired on 2026-10-26, not synthetic
market history. The index records actual acquisition times, parameters and
credits. Key-info retains only whitelisted quota fields; no headers, credentials,
account identifiers or raw key-info responses are included.

Rows outside BTC/ETH/SOL/USDT and Binance were removed from large identity and
market-pair responses. JSON was reencoded. `sha256` identifies each committed
file; `original_raw_sha256` identifies the complete original body in the ignored
local capture. Source values and provider timestamps were not rewritten.
The full source report uses the original body hashes.

The identity review was made at its recorded actual time for this validation
only. It grants no production identity or rule activation. Production manifests
remain empty/disabled.

Offline replay (no key, `.env` read, network, scanner or listener):

```powershell
.\.venv\Scripts\python.exe -m scripts.cmc_authenticated_validation --replay --source tests/fixtures/cmc_authenticated --bindings tests/fixtures/cmc_authenticated/bindings.reviewed.json --output research_runs/cmc_authenticated_replay.json
```

The cutoff is the final acquisition time, not the current replay wall clock.
Fields verified at that cutoff are historical evidence, not current quotes.
