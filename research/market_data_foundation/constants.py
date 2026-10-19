"""Fixed study limits. Caps are lowered where shared-IP spare capacity is unknown."""

from __future__ import annotations

PACKAGE = "research.market_data_foundation"
PHASE = "market-data-intelligence-foundation"

# Assignment hard caps. This phase uses the lowered online profile below them.
MAX_HTTP_ATTEMPTS = 800
MAX_CMC_ATTEMPTS = 12
MAX_COINPAPRIKA_ATTEMPTS = 4
MAX_COINGLASS_ATTEMPTS = 10
MAX_MCP_DATA_CALLS = 2
MAX_ONLINE_ROUNDS = 3
ONLINE_DEADLINE_SECONDS = 30 * 60
MAX_TOTAL_EVIDENCE_BYTES = 100_000_000
MAX_RESPONSE_BYTES = 8_000_000
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 1
MAX_RETRY_BACKOFF_SECONDS = 30.0
MIN_RETRY_BACKOFF_SECONDS = 1.0
MAX_CONCURRENCY = 2
MAX_CANDLE_ROWS = 1000
EVIDENCE_SCHEMA_VERSION = 1
# Spare shared-IP capacity was not established. Live sampling stays off.
LIVE_SAMPLING_AUTHORIZED = False
SHARED_IP_SPARE_CAPACITY = "unknown"

# Lowered because spare capacity of the shared Runtime/IP pool cannot be shown.
LOWERED_ONLINE_ROUNDS = 1
LOWERED_REASON = (
    "spare shared-IP capacity cannot be established; one finite public round, "
    "no websocket, no CoinGlass call, no full-universe scan"
)

TOP100_SIZE = 100
MONTH_LENGTH_DAYS = 30
FIVE_MINUTE_CYCLES_PER_DAY = 288
CLI_DEFAULT_WATCH_INTERVAL_SECONDS = 60.0

LIVE_DATABASE_NAME = "main_live_runtime.sqlite"
RUNTIME_CHECKOUT_SEGMENT = "candle craft inteligence"

DISPOSITIONS = (
    "verified",
    "missing",
    "stale",
    "malformed",
    "incompatible",
    "unverified",
    "unsupported",
    "access_blocked",
    "not_applicable",
)

CCI_MARK = {
    "missing": "N/A",
    "unverified": "Unverified",
    "stale": "N/A",
    "malformed": "Unverified",
    "incompatible": "Unverified",
    "access_blocked": "Unverified",
    "unsupported": "N/A",
    "not_applicable": "N/A",
    "verified": "verified",
}
