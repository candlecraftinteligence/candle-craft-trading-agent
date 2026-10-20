# Implementation plan

Inspect the existing clients, normalizers, cache, freshness policy, universe builder, and context consumers.
Add a research package that production `app` code does not import.
Reuse pure CCI functions and injected transports. Do not add a provider registry, schema migration, or scanner change.

## Safety

- No order execution, transfers, Telegram delivery, scanner watch loop, or live database.
- Output paths must resolve inside the DEV research root.
- Public quality score 88, grade A, public RR 3, internal minimum RR 2.5, schema 27, and zero freshness allowance stay as they are.
- CMC is a shadow reader. CoinPaprika remains the production rank and dominance source.
- Online caps are lowered because spare shared-IP capacity cannot be shown.
- Lowered-budget reason: spare shared-IP capacity cannot be established; one finite public round, no websocket, no CoinGlass call, no full-universe scan.

## Tests

Deterministic tests cover integrity, CMC envelopes, the rank boundary, budgets, path guards, and unchanged gate constants.
Live HTTP is opt-in through the CLI and is not part of `python -m pytest`.

## Completion

A runnable offline audit, committed sanitized contracts, and an honest online result or blocker.
