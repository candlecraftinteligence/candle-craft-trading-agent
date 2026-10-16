# Preserve outbox history during later schema migrations

## Scope

The schema-26-to-27 Runtime rehearsal for release
`085f8b73e7bce43316f02ef8b8910ab8cecca8f4` found that
`initialize_database` invoked the legacy schema-16 delivery-state conversion on
every initialization, including current-schema reopen. A populated schema-26
outbox exposed a case missing from the earlier migration fixture: a reserved,
pending event with no prepared payload, no message hash and zero attempts was
reclassified as uncertain. Five existing Runtime events changed on the isolated
copy. The preservation gate rejected that copy; production was not migrated.

## Repair

Run `_migrate_public_alert_delivery_state_v16` only when the source schema is
older than 16. Keep one call after the nullable-sent-at compatibility rebuild
and required delivery columns are available. The earlier duplicate call is
removed. Genuine legacy reservations still receive conservative uncertainty
classification. Later-schema rows retain their existing delivery state and
metadata, including incomplete historical evidence.

This does not add a schema version, retry an event, send a message, repair a
historical record, change signal policy or change the schema-27 receipt contract.
Preservation of an incomplete row does not establish transport provenance or
make it safe to resend. Missing historical evidence remains Unverified.

## Regression evidence

`tests/test_outbox_migration_preservation.py` uses the genuine schema-26 catalog
and synthetic rows only. It populates events, attempts and delivery parts with
pending, sent, uncertain, in-flight and final-failure states. It compares every
persisted table, including SQLite sequence values, through migration and reopen.
The only allowed historical row extension is the nullable schema-27 receipt
column. It also injects migration failures after column creation and version
assignment and verifies that outbox history and source schema remain intact.

All four new cases reproduce the failure on the unmodified parent release.
The focused suite includes the existing schema-14/15 migration tests, which
verify conservative legacy uncertainty, absence of resend eligibility and
idempotent migration. Synthetic test results do not constitute Runtime rollout
evidence.

## Next Runtime attempt

Select an exact reviewed, merged release containing this repair and PR #133,
with successful CI. Rehearse the supported migration on a verified isolated copy
of the actual source schema, checking all critical-table fingerprints, exact
locked-plan index definition, NULL historical receipts and idempotent reopen.

The previously rejected candidate is forensic evidence. Its old schema-26
backup manifest describes its bytes before the failed rehearsal and must not be
used as proof of a current backup. Production resumed on its unchanged compatible
release and has accumulated new history. A new deployment needs a current
consistent recovery boundary, matching external state, sufficient capacity and
a maintenance budget based on measured copy/check durations. Do not promote the
rejected candidate or restore an older outbox over new history.
