# Exact feature activation contract

This describes data for a future owner-reviewed Runtime staging packet. It is
not an approval or a DEV launch instruction. No new executable Runtime profiles
or APPROVED files are supplied by this PR.

The controller's checked-in `config/releases/runtime-profile-contract.json`
retains the immutable operational boundary: Runtime hostname, repository, state
root, complete epoch/generation identity and original scanner/listener arguments.
A separately staged `--contract` may update source profile hashes, release
identities and feature reviews, but cannot alter those boundary fields or relabel
DEV as Runtime. Both PowerShell roles forward `-ReleaseContract` and optional
`-ActivationPacket` to this same controller.

The new contract retains `baseline`, `implementation`, `rollback`, `profiles`,
`expected_identity` and all original operational fields. Add:

- `release_trees`: exact full 40-character SHA to full tree SHA mapping for every
  launchable release. Never use branch names or a length-only tree check.
- `profiles.<role>.environment_overrides`: complete exact reviewed source
  environment for a newly reviewed profile. The legacy format without this field
  still accepts only its original 36 overrides and CMC OFF. In either case the
  source bytes must match `profiles.<role>.sha256`, source baseline and identity.
- `activation_reviews.<role>`: SHA-256 of the role's canonical activation packet.
  This field is populated from reviewed data, not used to imply owner approval.

An activation packet has exactly these keys:

| Key | Required content |
|---|---|
| `role` | scanner or listener, matching the source profile |
| `release_sha`, `candidate_tree` | Exact staged launch code identity |
| `expected_identity` | Complete unchanged founding epoch/generation object |
| `source_profile_sha256` | Exact original profile bytes |
| `contract_sha256` | Canonical staged contract digest excluding only `activation_reviews` |
| `changes` | Nonempty map of allowed setting to exact `before`/`after` strings; absent source value is JSON null |
| `artifacts` | Exact identity/profile paths and raw-byte SHA-256 hashes when SHADOW is selected |

The only change keys are `TELEGRAM_SIGNAL_PRESENTATION` (legacy/precision),
`CMC_MODE` (off/shadow), `CMC_SIDECAR_PATH`, `CMC_IDENTITY_MANIFEST`,
`CMC_COLLECTION_PROFILE` (CUSTOM), `CMC_COLLECTION_PROFILE_PATH`,
`CMC_DAILY_CREDITS` (at most 388) and `CMC_MONTHLY_CREDITS` (at most 11,988).
All other overrides remain identical to the source. CMC decision/collect modes,
ranking-provider changes, credentials, storage-cap changes and arbitrary Python
settings are outside this activation allowlist.

An absolute feature file must stay inside the unchanged state root, contain no
parent traversal or alternate stream, and have the reviewed JSON/sidecar suffix.
The existing dedicated relative sidecar under `research_runs` is supported so
its original ledger need not be replaced. Sidecar and artifacts must not collide.
Any path or content change requires a new exact review and approval. File digests
are rechecked before launch; sidecars are never opened by the controller.

`digest` in `app.operations.release_activation` is canonical UTF-8 JSON SHA-256
(sorted keys, compact separators, ASCII escapes, no NaN). Source profile and
artifact hashes use their original bytes. Prepare derives a proposed profile
without approving it; the proposal is recomputed before launch and must match
exactly. It retains original source text for hash verification and records the
activation packet, unchanged arguments, epoch and untouched environment values.

Separate owner approval has exactly these keys:

`schema` (`cci-feature-approval/1`), `role`, `release_sha`, `candidate_tree`,
`expected_identity`, `profile_sha256`, `contract_sha256`, `activation_sha256`,
`changes`, `issued_at`, `expires_at`, `owner_approval_ref`.

Here `contract_sha256` includes the complete contract, including review hashes.
`profile_sha256` covers the complete derived proposed profile. `changes` must
equal the reviewed packet differences. `issued_at <= now < expires_at`, with a
maximum 24-hour approval window. OFF-only recovery with no activation packet
uses `digest(null)` as `activation_sha256` and an empty changes map; it still
requires the full exact approval. A setting change always requires its explicit
packet. Owner approval references must identify the separate real owner decision;
tests' SYNTHETIC references and hashes have no operational authority.

Code rollback additionally requires `rollback_compatibility` for the exact SHA
and tree, with complete retained-inventory and roundtrip receipt hashes, tested
plain/rich formats and sendMessage/sendRichMessage routes, and explicit evidence
for unchanged reservations, canonical deduplication, offset continuity, unknown
outcome withholding and current-inventory equality. The controller never invents
or gathers that Runtime evidence. Missing proof means controlled stop. Retaining
current code with CMC OFF, or selecting legacy for new messages, avoids a code
rollback while preserving rich reserved messages.
