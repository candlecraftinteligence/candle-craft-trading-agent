class FixtureContractError(ValueError):
    """A mission fixture failed the mock-source contract."""


class SetupNotFoundError(LookupError):
    """No fixture exists for the requested CCI setup id."""

    def __init__(self, cci_setup_id: str) -> None:
        self.cci_setup_id = cci_setup_id
        super().__init__(cci_setup_id)
