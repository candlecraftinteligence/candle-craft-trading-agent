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

## Architecture & Core Directives
- All agents (Cursor, Antigravity, and contributors) must read and follow [.cursor/rules/cci-core-architecture.mdc](.cursor/rules/cci-core-architecture.mdc) before modifying code, while respecting the higher-priority rules in this document and repository safety boundaries.

## Multi-Environment Operational Rules

### 1. ONE PHASE, ONE CODING ENVIRONMENT
Each CCI development phase is assigned exclusively to Cursor or Antigravity. Never develop the same phase simultaneously in both environments. A handoff requires explicit owner approval.

### 2. PHASE HANDOFF
At completion, provide branch, commit, scope, changes, tests, unresolved risks, and next-phase recommendations. Verify repository state before starting the next phase.

### 3. GIT AND WORKTREE SAFETY
Before modifying code, inspect the active branch, working tree, and linked worktrees. Do not overwrite other agents' work. Do not change branches, merge, push, reset, or modify other worktrees without approval. Never automatically merge into main.

### 4. DEV / PRODUCTION ISOLATION
Preserve all existing CCI runtime restrictions. No production database access, deployments, scanner/listener operations, live trading, or secret access without explicit authorization and established safeguards.

### 5. DEVELOPMENT AUTHORIZATION
Once the user explicitly assigns a phase, file edits within its approved scope may proceed. Production operations, destructive Git commands, and scope expansion require separate approval.

### 6. TESTING AND EVIDENCE
Use the existing project's Python environment. Execute relevant tests and report their real outcomes. Clearly disclose failures, skipped checks, and unresolved risks. Never claim phase completion without verification.
