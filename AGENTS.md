# AGENTS.md

Rules for all agents and contributors working on Candle Craft Trading Agent:

- Never hardcode secrets.
- Never implement live order execution unless explicitly requested.
- Never add withdrawal or transfer functionality.
- Use read-only exchange access by default.
- Keep modules small and testable.
- Add tests for every new feature.
- Use deterministic logic for trading signals.
- Mark missing data as N/A.
- Mark unreliable data as Unverified.
- Do not invent market data.
- Prefer rejecting weak setups over generating many alerts.
- All trade ideas must include invalidation and risk warning.
- All API clients must handle rate limits, timeouts, and bad responses.
- All new code must pass pytest.
