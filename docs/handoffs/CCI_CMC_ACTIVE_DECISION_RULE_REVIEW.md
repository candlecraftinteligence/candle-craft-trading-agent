# CCI CMC active decision rule review

Review date: 2026-10-26. Scope: Scanner V1, PR #148. No live rule activation.
This review uses the [authenticated evidence](../research/cmc_authenticated_validation.json),
the [five-mode release fixture](../research/cmc_release_regression.json), and
`app/cmc/policy.py`, `features.py`, `comparison.py`, `integration.py` and
`selection_audit.py`. Saved market observations are evaluated at their recorded
cutoff, not represented as currently fresh.

## Rule classification

YES in the causal column means the engine rejects future acquisition/source
times and the recorded required context was causal at its validation cutoff.
It does not establish a historical outcome study or present Runtime freshness.
Experiment means observational SHADOW unless separately authorized otherwise.

| Rule / version | SOURCE_VERIFIED | CAUSALLY_VALID | DETERMINISTICALLY_TESTED | ECONOMICALLY_VALIDATED | ELIGIBLE_FOR_EXPERIMENT | ELIGIBLE_FOR_PRODUCTION_ENFORCEMENT | BLOCKED |
| --- | --- | --- | --- | --- | --- | --- | --- |
| data_divergence/1 | NO for a usable real equivalent comparison | Contract supported; actual comparison insufficient | YES | NO | Synthetic mechanics only | NO | OI counting, funding kind/cadence, price units/time alignment; no economic evidence or approval |
| liquidation_stress/1 | YES for timestamped global USD aggregates | YES at recorded cutoff | YES | NO | YES, SHADOW aggregate-stress hypothesis | NO | Rejected-winner analysis, net expectancy, adverse-regression attestation and exact owner approval |
| adverse_regime/1 | YES for global cap change and liquidation totals | YES at recorded cutoff | YES, including actual CONFIRMED delivery tie ordering | NO | YES, first priority hypothesis in SHADOW | NO | Prospective/out-of-sample evidence, acceptable coverage/budget, exact rule approval |
| sector_support/1 | NO | NO for current stale sector/basis inputs | YES mechanically | NO | Synthetic mechanics only | NO | Fresh causal sector membership/returns and reviewed index/basis units, then economic validation |
| consistent_context/1 | NO | NO usable real equivalent comparison plus basis | YES mechanically | NO | Synthetic mechanics only | NO | Equivalent comparison and index/basis semantics, then economic validation |

No rule is economically validated. Approved rules: **NONE**. Enabled production
rules: **NONE**. `config/cmc/activations.disabled.json` remains `[]`; the production
identity manifest remains empty. A SecretStr credential and an accessible API
route grant no rule authority.

## Actual rule inputs and authority

- `data_divergence`: `CmcDataConsistency == -1`. Requires the exact same contract,
  scope, metric, units, OI counting or funding semantics, fresh causal timestamps
  and source skew <=30 seconds. The real sample produced seven UNVERIFIED and
  two UNIT_MISMATCH results; it cannot establish that Binance is wrong.
- `liquidation_stress`: pressure >=4 and direction-signed imbalance >=0.75.
  Pressure is 24 times rolling global 1h total divided by rolling global 24h
  total; imbalance is (long-short)/(long+short). These are global aggregate
  conditions. They prove neither a local stop sweep nor a profitable veto.
- `adverse_regime`: direction-signed negative 24h global cap return >3 percent
  and pressure >=2. This yields a priority delta of -1, never a score change.
  It needs neither CMC contract OI nor funding, index, basis or sector data.
- `sector_support`: direction-signed sector relative return >=2 percentage
  points, supportive broad cap return and absolute verified basis <=0.005.
  Membership and return must both be causal. Current captured sector dates are
  stale; current index/basis semantics are incomplete.
- `consistent_context`: equivalent sources agree and absolute verified basis
  <=0.005. CONFIRMED_SUPPORT is an annotation; it cannot confirm a lifecycle state.

## Where priority can actually act in current V1

Current `PUBLIC_INITIAL_ALERT_BY_STATE` admits TRIGGERED and CONFIRMED events;
initial WATCHLIST publishing is not an eligible transition. The original CMC
hook in legacy WATCHLIST sorting therefore did not establish active priority
under the current lifecycle policy. This review does not enable that old path.

The release adds a bounded delivery-order tie-break for already eligible, new
CONFIRMED candidates. It moves only occupied slots with identical four-component
canonical ranks: quality score, RR, technical score and opportunity score.
Within those ties an individually authorized priority delta determines order;
remaining ties retain original order. Different canonical ranks, rejected
candidates and prior-public follow-ups retain their positions/ownership.
OFF/COLLECT/SHADOW and unapproved DECISION preserve the baseline order.

This changes **order only**, not the set or count of eligible CONFIRMED sends.
It introduces no new per-scan cap, no additional signal and no artificial
WATCH-to-CONFIRMED promotion. The existing policy's delivery limits remain its
own authority. A hypothetical finite-capacity research selection is explicitly
separate from this actual delivery behavior. Displacement in production must
not be claimed merely because a synthetic capacity fixture displaced a plan.

Hard vetoes remain separately guarded. They apply only to newly eligible public
candidates, preserve setup/plan/event IDs and never suppress existing follow-ups.
They require more economic evidence than an ordering experiment. There is no
automatic activation of either veto.

## Exact formula identities

| Rule | Version | SHA-256 specification hash |
| --- | --- | --- |
| data_divergence | 1 | `580bd1a4824a2ea4452ce29b742dd86e60171f7ca066891f5a925d61b20736fe` |
| liquidation_stress | 1 | `e8bcc2c51898385dc345093aaa92cb9d72faece8b2f12e6e53b6ec3dc602a735` |
| adverse_regime | 1 | `0cd7878eb32d7a66f4b5a62a6f423b5762d16694b3a6ca37fae93c2583fbcf44` |
| sector_support | 1 | `b1e8d1132d5833db7d2aff5b181d6e6a935b80d7f56034faf38b8fc2811bb2bd` |
| consistent_context | 1 | `31d22a811e04632bc8f347a35f37188cf164d5ac2542d24e5c25ac8db0467510` |

The existing activation contract is unchanged: one enabled entry for the exact
rule/version/hash; complete source, causal, independent outcome, test, hard-gate,
baseline and owner references; affirmative no-material-negative-regression;
timezone-aware approval and expiry; approval <= selection < expiry. Duplicate,
expired or incomplete entries cannot enforce. Synthetic test approvals in the
fixture are not production approvals.

Next required work and release authorization are specified in the
[activation plan](CCI_CMC_DECISION_ACTIVATION_PLAN.md). The default credit budget
does not support continuous full-family acquisition at five-minute cadence;
source availability, budget and representative coverage must be demonstrated
before interpreting SHADOW performance or enabling a rule.
