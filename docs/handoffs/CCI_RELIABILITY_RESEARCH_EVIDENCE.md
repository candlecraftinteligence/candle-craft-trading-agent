# CCI reliability: research evidence handoff

Date: 2026-10-23. State: **CODE_COMPLETE**. No merge or rollout authorization.

- Branch: `codex/reliability-research-evidence`.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/142.
- Stacked base: `bdcc90e649fdcd4d5b542f867ba27264a6d038d9` (PR #141).
- Verified implementation/test head: `30f8d0f38d13949f869c1c9dbd7d4b647192a335`.
- Commits: `0220a0f`, `25cc560`, `30f8d0f`. The documentation-only final head is
  recorded in the PR after publication and receives its own exact-head CI/review.

## Problem and correction

Plan attribution and diagnostic snapshot interpretation did not expose supplied
execution-integrity uncertainty. A synthetic Unverified progress row with a gap
diagnostic could have an interpretable TP3 snapshot when identity/context matched.
This reproduced a reporting-classification issue, not lifecycle fabrication.

A small pure helper retains source integrity claims, diagnostics, timestamps,
tracking boundaries and lineage. Evaluations and plan interpretations now expose
conservative evidence strata and separate counts. Reported Verified remains a
source claim; historical completeness is Unverified. Missing data stays N/A.
Existing outcome, identity, context and unavailable expectancy/P&L meanings remain.
No source records, economics, cursor or trading outcome are rewritten.

## Verification

- Full existing-environment pytest on `30f8d0f`: **3405 passed, 1 warning**, 633.88 s;
  no failures/skips. Existing FastAPI/Starlette TestClient httpx deprecation warning.
- Corrected timeout/report/attribution/context/cutoff subset: 108 passed.
- Independent Astra APPROVE for research implementation `25cc560`: 89 targeted
  tests independently passed. Astra APPROVE for exact `30f8d0f`; corrected timeout
  regression independently passed.
- Required CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39948528496
  succeeded on `30f8d0f`. Dependency, compile and whitespace checks passed.
- Earlier full run on `25cc560`: 3404 passed, one failed. An existing scanner test
  timed real CPU work against 0.2 s; it passed alone. The test now uses prepared
  real results and a blocked synthetic symbol to exercise the actual timeout and
  continuation path under a bounded outer deadline. Production scanner/timeouts
  are unchanged. The final full run above verifies the corrected test.
- Diff confirms strategy, lifecycle, public delivery, quality 88/A/3 and P4
  configuration unchanged. No schema or dependency change.

## Risks and continuation

The 73 historical continuity gaps and 20 missing boundaries remain unresolved.
No historical data was acquired, fabricated or relabelled. Runtime coverage,
resources and historical delivery remain N/A / Unverified from DEV. Tests and
reviews do not establish Runtime improvement or authorize research expectancy.

Next: focused Telegram provenance diagnostics (D), with bounded read-only event-key
classification, durable incomplete/unresolved reporting and bounded repeated logs.
Preserve canonical claims, deduplication and valid recovery; no historical resend,
receipt fabrication or persisted quarantine mutation. Then address scanner
start/exit coverage (A) and phase/resource observations (E). Runtime access, merge
and deployment require their separate approvals and exact release verification.
