"""Version and status vocabulary for durable source replay.

The evidence store schema is independent of the operational application schema.
"""

from __future__ import annotations

CODEC_VERSION = "cci-durable-source-replay-codec-v1"
STORE_SCHEMA_VERSION = 1
STORE_FORMAT = "cci-durable-source-replay-store-v1"
# Breaking attestation change: expanded semantic implementation closure.
IMPLEMENTATION_ATTESTATION_VERSION = "cci-durable-source-replay-impl-v2"
LIVE_RUNTIME_DB_NAME = "main_live_runtime.sqlite"
MAX_PARENT_DEPTH = 8
SQLITE_HEADER_BYTES = 16
# Empty evidence schema plus indexes already exceeds a few dozen KiB.
MIN_USABLE_STORE_BYTES = 48 * 1024

CALLER_LIFECYCLE_SERVICE = "app.lifecycle.service._apply_to_symbol_result_with_meta"
CALLER_OWNER_MONITORING = "app.lifecycle.owner_monitoring.monitor_tracking_obligations"

LINEAGE_SERVICE = "lifecycle_service_symbol_result"
LINEAGE_SCANNER_SUPPLIED = "scanner_supplied_without_delivery_envelope"
LINEAGE_FETCHED = "fetched_without_delivery_envelope"
LINEAGE_SUPPLIED = "supplied_without_delivery_envelope"

CAPTURE_COMPLETE = "COMPLETE"
CAPTURE_INCOMPLETE = "INCOMPLETE"

TX_ENCLOSING_COMMITTED = "enclosing_committed"
TX_ENCLOSING_ROLLED_BACK = "enclosing_rolled_back"
TX_SAVEPOINT_ROLLED_BACK = "savepoint_rolled_back"
TX_COMMIT_INTERRUPTED = "commit_interrupted"
TX_COMMIT_UNKNOWN = "commit_unknown"

TX_STATUS_VALUES = frozenset(
    {
        TX_ENCLOSING_COMMITTED,
        TX_ENCLOSING_ROLLED_BACK,
        TX_SAVEPOINT_ROLLED_BACK,
        TX_COMMIT_INTERRUPTED,
        TX_COMMIT_UNKNOWN,
    }
)

SAVEPOINT_OPEN = "open"
SAVEPOINT_RELEASED = "released"
SAVEPOINT_ROLLED_BACK = "rolled_back"
SAVEPOINT_NONE = "none"

SAVEPOINT_DISPOSITION_VALUES = frozenset(
    {
        SAVEPOINT_OPEN,
        SAVEPOINT_RELEASED,
        SAVEPOINT_ROLLED_BACK,
        SAVEPOINT_NONE,
    }
)

CAPTURE_STATUS_VALUES = frozenset({CAPTURE_COMPLETE, CAPTURE_INCOMPLETE})

REPLAY_MATCH = "REPLAY_MATCH"
REPLAY_MISMATCH = "REPLAY_MISMATCH"
EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
EVIDENCE_CORRUPT = "EVIDENCE_CORRUPT"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
UNSUPPORTED_IMPLEMENTATION_OR_POLICY = "UNSUPPORTED_IMPLEMENTATION_OR_POLICY"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INCOMPLETE = 2
EXIT_CORRUPT = 3
EXIT_UNSUPPORTED_FORMAT = 4
EXIT_UNSUPPORTED_IMPLEMENTATION = 5
EXIT_MISMATCH = 6

EXIT_BY_STATUS = {
    REPLAY_MATCH: EXIT_OK,
    REPLAY_MISMATCH: EXIT_MISMATCH,
    EVIDENCE_INCOMPLETE: EXIT_INCOMPLETE,
    EVIDENCE_CORRUPT: EXIT_CORRUPT,
    UNSUPPORTED_FORMAT: EXIT_UNSUPPORTED_FORMAT,
    UNSUPPORTED_IMPLEMENTATION_OR_POLICY: EXIT_UNSUPPORTED_IMPLEMENTATION,
}

# Storage-generated columns. The evaluator never reads them.
# Progress is addressed by lifecycle_id + plan_identity. Event order is
# timestamp then insertion ordinal; event_id only breaks timestamp ties and is
# preserved for rows that already existed by seeding those ids.
NONSEMANTIC_PROGRESS_COLUMNS = ("id", "created_at", "updated_at")
NONSEMANTIC_EVENT_COLUMNS = ("event_id",)

PAYLOAD_CALL = "call_inputs"
PAYLOAD_PRESTATE = "prestate"
PAYLOAD_DELIVERY = "delivery"
PAYLOAD_POLICY = "policy"
PAYLOAD_RESULT = "result"
PAYLOAD_EFFECTS = "effects"
