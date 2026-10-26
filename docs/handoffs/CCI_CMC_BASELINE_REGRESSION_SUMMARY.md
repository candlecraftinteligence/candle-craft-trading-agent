# CCI CMC baseline regression summary

Scope: PR #148 release review on the existing feature branch. All execution is
DEV/offline or GitHub CI, with mocked exchange/Telegram transports and isolated
test databases. No live scanner, listener, Runtime database or production rule
was started. Use the verification manifest and PR checks for the exact tested SHA.

## Machine-readable comparisons

[Release regression artifact](../research/cmc_release_regression.json) is generated
by `tests/cmc_release_fixture.py`. It contains BASELINE_CCI, CMC_OFF, CMC_COLLECT,
CMC_SHADOW and CMC_DECISION, six policy scenarios, exact evaluations/formula
hashes, introduced/displaced research plan IDs, and local resource measurements.

The current mocked Scanner V1 run's protected output matches the independently
saved main fixture in all four CMC modes. Normalization removes only the explicit
mode label when comparing canonical outputs. It compares scanner acceptance,
scores, rejected/valid modes, rejection gates, geometry, public eligibility,
economic plan and delivery identity. It does not remove a changed strategy field
to make a comparison pass. OFF also retains its original serialized schema.

The policy scenarios cover:

1. Actual captured verified global/liquidation context at its original cutoff,
   with no production activation. No actual market timestamp is rewritten.
2. Synthetic adverse context with the priority rule disabled.
3. Synthetic individually authorized `adverse_regime/1` tie-breaking.
4. Synthetic individually authorized `liquidation_stress/1` hard veto.
5. Missing provider evidence.
6. Expired evidence/approval.

Synthetic capacity comparisons are mechanics tests. They are not actual fills,
real economic results or approval to activate a rule. Actual current CONFIRMED
delivery priority changes only the order of equal canonical candidates; it does
not remove a candidate or introduce a new capacity limit.

## Boundary invariants covered

| Invariant | Evidence |
| --- | --- |
| Rejected setups cannot be promoted | Policy no-promotion cases; original Scanner V1 output parity; public quality suite |
| Canonical quality precedes CMC | Exact four-component rank grouping; actual public delivery tests with equal and unequal quality |
| No added signal count/cap bypass | Actual CONFIRMED baseline and ordered sends have equal count; existing public limit/dedup tests |
| OFF/COLLECT remove old authority | New reused-result mode-switch tests through actual public delivery |
| Missing, stale, future, incompatible data give no authority | Evidence/identity/unit/time tests and real saved response replay |
| Exact rule/version/hash approval required | Existing activation expiration, duplicate, missing-reference and hash mismatch cases |
| Veto affects only new public candidates | Existing real delivery modes, prior-public and follow-up tests |
| Setup/economic-plan/event identities stable | Protected scanner/public fixture hashes and before/after public result comparisons |
| API outage cannot halt canonical scan | Existing background/coalescing/failure tests and all-mode mocked scanner test |
| CMC sidecar failures preserve baseline | Failed policy write and failed selection-intent write tests |
| Applied vs hypothetical distinguishable | Policy batch, separate selection intent, actual order, application/delivery status; no fill inference |
| No execution, transfer or live-service behavior | Fixed public GET endpoint registry, changed-file review, default safety settings and tests |

## Verification commands and results

Use the existing `.venv` with process-local safe flags:

```powershell
$env:LOCAL_MANUAL_MODE='true'
$env:ORDER_EXECUTION_ENABLED='false'
$env:TELEGRAM_DRY_RUN='true'
$env:TELEGRAM_SIGNALS_ENABLED='false'
$env:TELEGRAM_RESEARCH_WATCH_ENABLED='false'
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe tests\cmc_release_fixture.py research_runs\cmc_release_regression.json
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q app scripts src tests
```

The release adds 16 CMC regression cases to the prior 114: **130 CMC cases**,
**3,683 total cases**, **180 test files**. Final local checks passed all 130 CMC
cases, all 171 cases in the affected lifecycle/CMC module run, and all 3,683
distinct full-suite cases. Full partitions: 880 + 827 + 995 + 981, zero failures,
errors or skips, with one existing Starlette/httpx deprecation warning. Unique
test IDs were checked. [Verification evidence](../research/cmc_release_verification.json)
records exact results and JUnit hashes. Dependencies and full source compilation
passed. Exact-head GitHub CI is a separate required merge gate.

Initial release-wide local run: 3,679 passes and four cooldown-test failures.
The cause was an added unconditional clock call in the OFF delivery path, which
consumed deterministic test clocks and changed baseline clock-call behavior.
That call is now conditional on SHADOW/DECISION. Original tests and assertions
were retained. Earlier new ranking fixtures also revealed that legacy WATCHLIST
initial events are not eligible in current V1; the fixture and actual priority
boundary were corrected to use existing CONFIRMED delivery, not a relaxed gate.
Both affected-module and full reruns passed after the correction.

## Resource interpretation

The artifact records a local 100-candidate feature/policy workload with 1,513
snapshot observations, traced Python allocation peak and separate traced/untraced
wall time: approximately 0.303 seconds untraced for 100 candidates and 200,432
bytes peak incremental Python allocation under tracing on DEV. It excludes
provider requests and does not establish process RSS,
Runtime p95 latency or fitness of the 4 GB host. The full integration still needs
the approved Runtime workload/capacity measurements in the rollout plan.

Collection bounds, cache/inflight limits, HTTP deadlines, storage/credit caps and
failures are tested separately. A local benchmark, a green CI run and a working
transport do not establish profitable decision rules or approve deployment.
