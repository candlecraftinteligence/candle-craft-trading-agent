# Candle Craft cleanup manifest - 2026-08-15

Baseline commit: `aca528fe7a227eb43146120ffecb168cb439994f`
Working branch: `audit/candle-craft-reliability-validation`
Scope: tracked repository content only

## Safety boundary

This manifest does not authorize deletion of untracked or ignored files, research data, migrations, remote branches/tags/PRs, or Runtime artifacts. In particular, `.env`, `.env.backup_*`, all ignored `scan_runs` data, and `scan_runs/main_live_runtime.sqlite` are excluded from cleanup. The protected Runtime database must not be inspected, migrated, copied, checkpointed, vacuumed, repaired, or deleted on Dev.

Current `git status --short --branch --ignored` shows the protected local data as ignored. No ignored file will be staged or changed.

## Approved tracked cleanup items

| ID | Change | Evidence | Risk | Recovery | Validation |
|---|---|---|---|---|---|
| CLN-001 | Delete the first of two consecutive `_public_setup_quality_score_decimal` definitions in `app/alerts/telegram_lifecycle.py`. | AST inventory found the same top-level name at lines 8405 and 8416. The two bodies are byte-for-byte equivalent in current source; Python silently binds only the latter. Repository-wide AST inventory found no other duplicate top-level class/function names. | Low: removing the shadowed definition should not alter runtime behavior. Public quality eligibility is high impact, so focused Telegram/public-gate tests are mandatory. | Revert the cleanup commit, or restore the removed block from baseline commit `aca528f`. | Focused public signal, lifecycle delivery, sender, and alert-integrity tests; full pytest; compileall; `git diff --check`. |
| CLN-002 | Normalize UTF-8 encoding by removing the BOM from `src/x_hype_prompt_agent/prompt_builder.py` and `tests/test_prompt_builder.py`; textual content remains unchanged. | A tracked-path-only byte inventory found BOMs only in these two Python files. Raw `utf-8` AST parsing fails on the production file with U+FEFF, while `utf-8-sig` succeeds. | Low: Python accepts the BOM today, but some static tooling does not. Byte normalization could expose line-ending churn, so the diff must show only the BOM removal. | Revert the cleanup commit, or restore exact bytes from `aca528f`. | X-hype prompt-builder tests; compileall; inspect `git diff --word-diff=porcelain`; full pytest. |
| CLN-003 | Add shared ignore rules for `scan_runs/*.db`, `scan_runs/*.sqlite`, `scan_runs/*.sqlite3`, and SQLite sidecars. No file deletion. | Current database protection depends on this checkout's private `.git/info/exclude`; the tracked `.gitignore` lacks SQLite patterns. A fresh clone could accidentally expose Runtime/test databases to staging. | Low: tracked source is unaffected. Overbroad global SQLite ignores are avoided; rules are scoped to `scan_runs`. | Revert the cleanup commit. | `git check-ignore` against disposable path names only; `git status --ignored`; `git diff --check`. |

## Retained after audit

| Content | Disposition and evidence |
|---|---|
| M5/`5m` fields and tests | Retain. M15 is the default and default scanner tests prove no M5 fetch. Explicit M5 is a documented research override, and historical M5 lifecycle/scan data must remain readable. The legacy enum value `SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED` is a stored compatibility value; current transition notes use the configured timeframe. |
| Root implementation/audit Markdown files | Retain. They are tracked historical/project evidence and no authoritative ownership or supersession proof establishes safe deletion. Consolidation may be proposed later without deleting history. |
| Alembic migrations and schema compatibility code | Retain. Migration history and v14/v15/v16 preservation are tested and explicitly protected. |
| Requirements | Retain. FastAPI, SQLAlchemy, Alembic, pydantic-settings, python-dotenv, httpx, and pytest are directly used; uvicorn and psycopg are documented/runtime entrypoint and SQLAlchemy-driver dependencies. The absence of a lock/constraints artifact remains a reproducibility finding, not evidence that a dependency is unused. |
| `scan_runs/.gitkeep`, `replay_reports/.gitkeep`, `replay_validation/.gitkeep` | Retain. They preserve intentional output directories while generated content remains ignored. |
| Current ignored research/runtime artifacts | Retain untouched. They are not cleanup candidates and are not evidence of tracked-content contamination. |

## Tracked-artifact evidence

The clean baseline plus the non-ignored `rg --files` inventory establishes the current tracked/non-ignored tree. No current tracked `*.sqlite`, `*.sqlite3`, `*.db`, `*.log`, `*.zip`, `*.pyc`, generated scan JSON, or JSONL artifact was found. History shows generated `scan_output.json` and `scan_runs/latest_scan.json` were removed in commits `0a7156a` and `ec42ac9`; they must not be reintroduced.

## Cleanup acceptance gate

Cleanup is accepted only if:

1. the diff contains only CLN-001 through CLN-003;
2. protected/untracked files remain untouched and unstaged;
3. focused tests and the complete suite pass;
4. compile/import/dependency/diff checks pass;
5. public-signal eligibility output is unchanged by CLN-001;
6. the cleanup is committed separately from reliability and strategy changes.

If any gate fails, revert the cleanup commit; do not compensate by weakening scanner, lifecycle, target, scoring, or Telegram gates.
