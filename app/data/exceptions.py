from __future__ import annotations


class ExchangeClientError(Exception):
    """Base exception for public exchange market-data clients."""


class ExchangeTimeoutError(ExchangeClientError):
    """Raised when an exchange request times out."""


class ExchangeNetworkError(ExchangeClientError):
    """Raised when a request fails before a response is received."""


class ExchangeHTTPError(ExchangeClientError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ExchangeRateLimitError(ExchangeClientError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ExchangeResponseError(ExchangeClientError):
    """Raised when an exchange returns unusable response content."""


class ExchangeMalformedJSONError(ExchangeResponseError):
    """Raised when a response body cannot be decoded as JSON."""


class ExchangeMissingFieldError(ExchangeResponseError):
    """Raised when a required field is absent from an exchange payload."""
