# Lifecycle geometry quarantine

Malformed stored lifecycle plans are unsafe to interpret. A stored lifecycle can only
enter or remain in an outcome-eligible state when it has a supported `long` or `short`
direction, finite entry/stop/three-target levels in the correct directional order,
invalidation text, and a stable matching plan identity.

The scanner, lifecycle outcome evaluator, public-eligibility checks, and Telegram
decision path fail closed when this condition is not met. They do not infer missing
direction or price levels from current market data. In particular, malformed geometry
cannot create outcome progress, public eligibility, a Telegram reservation, delivery,
or an order action.

## Audited maintenance workflow

The hygiene command requires an explicit database path and is read-only by default:

```powershell
.\.venv\Scripts\python.exe scripts\repair_lifecycle_hygiene.py `
  --database-path C:\path\to\disposable-copy.sqlite `
  --json-output C:\path\to\geometry-audit.json
```

Review the JSON plan and its human-readable stderr report. It verifies schema v16,
required lifecycle indexes, `quick_check`, `integrity_check`, and
`foreign_key_check` before classifying every malformed stored plan as one of:

- `safe_to_quarantine`: pre-public `NOT_A_GRADE_CANDIDATE` lifecycle with no delivery,
  execution, or valid/ambiguous outcome-progress dependency. `WATCHLISTED` and
  `STALKING` transition legally to `REJECTED`; `TRIGGERED`, `CONFIRMED`, and
  A-grade monitoring states transition legally to `INVALIDATED`.
- `historical_preserve`: terminal or otherwise non-outcome-eligible evidence. It is
  left unchanged and remains queryable, including malformed progress history.
- `requires_manual_review`: any public delivery, reservation/uncertainty, execution or
  managing state, non-candidate actionability, valid/partial outcome progress, or
  unknown/ambiguous situation. The command never repairs these rows automatically.

Only after reviewing an audit for a disposable copy, apply the exact reviewed plan:

```powershell
.\.venv\Scripts\python.exe scripts\repair_lifecycle_hygiene.py `
  --database-path C:\path\to\disposable-copy.sqlite `
  --apply `
  --confirm legacy_invalid_stored_plan_geometry `
  --archive-directory C:\path\to\verified-backups `
  --json-output C:\path\to\geometry-apply.json
```

`--apply` requires the exact confirmation token. It creates a verified backup unless
an operator explicitly supplies `--no-backup` for controlled testing. The apply phase
re-audits inside one `BEGIN IMMEDIATE` transaction, rejects a stale plan, conditionally
matches lifecycle ID, state, plan identity and original malformed geometry, then writes
only the legal lifecycle-state update and one audit event with reason code
`legacy_invalid_stored_plan_geometry`. Any unexpected condition rolls back the whole
operation. Post-apply integrity checks run before and after commit.

The command never deletes, prunes, rewrites, or fabricates lifecycle outcome analytics,
Telegram attempts, public events, delivery parts, scanner rows, direction, price
levels, or invalidation geometry. A second apply is intentionally a no-op.

Recommended operating sequence:

1. Audit a disposable copy and review the complete JSON plan.
2. Apply to that disposable copy with deliberate confirmation.
3. Run application initialization twice against the copy, then verify integrity and
   counts.
4. Obtain separate authorization before any Runtime operation; this command does not
   select or discover a Runtime database.
