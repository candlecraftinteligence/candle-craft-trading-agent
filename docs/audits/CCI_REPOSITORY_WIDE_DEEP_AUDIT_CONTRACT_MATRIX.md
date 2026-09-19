# CCI Deep Audit — Forensic Contract Matrix (supporting table)

Companion to `docs/audits/CCI_REPOSITORY_WIDE_DEEP_AUDIT.md`. Audited SHA `eef92b89f168bbb016715f3486b9f6bf2824f53f`. Schema **v25**. Investigation only.

| Contract | Class | Production-bound? | Primary code | Primary tests |
| --- | --- | --- | --- | --- |
| P0 Evidence Contract | SOUND (dictionary) | Labels only | `app/analytics/evidence_contract.py` | `tests/test_evidence_contract.py` |
| P1 Immutable Economic Plan Identity | PARTIAL | Latched on lifecycle write; consumers not migrated | `app/lifecycle/economic_identity.py` | `tests/test_economic_identity.py` |
| P2A Activation Accounting | SOUND (scoped) | Read-only projection | `app/analytics/activation_accounting.py` | `tests/test_activation_accounting_truth.py` |
| P2B Lifecycle Ownership / oscillation | SOUND prospective / PARTIAL historical | Yes, state machine | `app/lifecycle/state_machine.py` | `tests/test_lifecycle_ownership_repair_p2b.py` |
| P3A Outcome Ownership | PARTIAL | Diagnostic only | `app/analytics/outcome_ownership.py` | `tests/test_outcome_ownership_contract.py` |
| P3B1 Plan attribution | PARTIAL | First INSERT only | `app/lifecycle/repositories.py` | `tests/test_outcome_plan_attribution_p3b1.py` |
| P3B2A Evaluation Context | SOUND boundary / PARTIAL completeness | `complete` hard-coded False | `app/analytics/outcome_ownership.py` | `tests/test_evaluation_context_p3b2a.py` |
| P3B2B Eligibility Cutoff | SOUND last-pass / PARTIAL original clock | Yes, progress columns | `app/lifecycle/outcomes.py` | `tests/test_durable_eligibility_cutoff_p3b2b.py` |
| P3_PREFIX | PARTIAL | Last-pass envelope | `app/lifecycle/prefix_disposition_evidence.py` | `tests/test_prefix_disposition_evidence_p3_prefix.py` |
| R0 Observation Unit | PARTIAL spec | **No admission persistence** | `docs/research/observation_unit_contract_r0.md` | `tests/test_observation_unit_contract_r0.py` |
| EVAL_SEMANTICS | SOUND non-equivalence proof | Does not bind | `docs/research/evaluation_semantics_contract.md` | `tests/test_evaluation_semantics_contract.py` |
| STORAGE_SINGLE_COPY | PARTIAL | Prospective `symbol_refs_v1` | `app/storage/scan_payloads.py` | `tests/test_storage_single_copy.py` |
| EVALUATION_POLICY_MANIFEST | UNBOUND | Not imported by evaluators | `app/research/evaluation_policy.py` | `tests/test_evaluation_policy_manifest.py` |
| SOURCE_EVIDENCE_BOUNDARY | UNBOUND library / PARTIAL in-memory | Descriptor always unbound | `app/research/source_evidence.py` | `tests/test_source_evidence_boundary.py` |

Historical-issue rollup: duplication PARTIALLY FIXED; CONFIRMED oscillation FIXED prospectively; activation PARTIALLY FIXED; RR wiring FIXED with residual floor inconsistency; empty replay STILL PRESENT semantically; source provenance PARTIALLY FIXED in-memory; cutoff vs receipt STILL PRESENT; evaluator serialization STILL PRESENT; expectancy denominator STILL OPEN (R0).
