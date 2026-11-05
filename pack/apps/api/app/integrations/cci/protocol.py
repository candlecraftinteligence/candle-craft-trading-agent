from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from app.integrations.cci.dto import CciEventDTO, CciSetupDTO


@runtime_checkable
class CciMissionSource(Protocol):
    """Read-only CCI contract.

    The Pack never writes lifecycle, never executes orders, and never
    treats this protocol as a trading gateway. Phase 2 may add a live
    reader. This build ships MockFixtureSource only.
    """

    def list_open_setups(self) -> list[CciSetupDTO]:
        """Setups whose fixture events do not include SETUP_RESOLVED."""

    def get_setup(self, cci_setup_id: str) -> CciSetupDTO:
        """Return one setup or raise SetupNotFoundError."""

    def iter_events_since(self, cursor: str | None) -> Iterable[CciEventDTO]:
        """Events after `cursor`.

        `None` yields every event. A known `cci_event_id` yields events
        strictly after that id in time order. Any other string is treated
        as an ISO timestamp and yields events with a later `occurred_at`.
        """
