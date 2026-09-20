"""PROSPECTIVE_RUNTIME_EPOCH_ISOLATION: durable operational epoch boundary."""

from app.runtime_epoch.authority import (
    initialize_runtime_epoch,
    load_active_runtime_epoch,
    require_active_runtime_epoch,
    require_expected_runtime_epoch,
)
from app.runtime_epoch.cohort import (
    classify_lifecycle_cohort,
    classify_public_cohort,
    classify_run_cohort,
)
from app.runtime_epoch.errors import (
    RuntimeEpochConfigurationError,
    RuntimeEpochError,
    RuntimeEpochIdentityCollisionError,
    RuntimeEpochOriginError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import (
    COHORT_CURRENT_EPOCH_OPERATIONAL,
    COHORT_LEGACY_OR_UNATTRIBUTED,
    RUNTIME_EPOCH_CONTRACT_VERSION,
    RuntimeEpochIdentity,
    RuntimeEpochRecord,
    SymbolOriginDecision,
)
from app.runtime_epoch.origin import evaluate_symbol_origin, register_operational_run
from app.runtime_epoch.startup import (
    OperationalContext,
    OperationalRuntime,
    identity_from_settings,
    inspect_operational_database,
    migrate_existing_database,
    open_operational_context,
    open_operational_database,
    open_operational_service_database,
    open_repository_database,
    require_expected_operational_identity,
    require_operational_runtime,
)

__all__ = [
    "COHORT_CURRENT_EPOCH_OPERATIONAL",
    "COHORT_LEGACY_OR_UNATTRIBUTED",
    "RUNTIME_EPOCH_CONTRACT_VERSION",
    "OperationalContext",
    "OperationalRuntime",
    "RuntimeEpochConfigurationError",
    "RuntimeEpochError",
    "RuntimeEpochIdentity",
    "RuntimeEpochIdentityCollisionError",
    "RuntimeEpochOriginError",
    "RuntimeEpochOwnershipError",
    "RuntimeEpochRecord",
    "SymbolOriginDecision",
    "classify_lifecycle_cohort",
    "classify_public_cohort",
    "classify_run_cohort",
    "evaluate_symbol_origin",
    "initialize_runtime_epoch",
    "load_active_runtime_epoch",
    "identity_from_settings",
    "inspect_operational_database",
    "migrate_existing_database",
    "open_operational_context",
    "open_operational_database",
    "open_operational_service_database",
    "open_repository_database",
    "register_operational_run",
    "require_expected_operational_identity",
    "require_active_runtime_epoch",
    "require_expected_runtime_epoch",
    "require_operational_runtime",
]
