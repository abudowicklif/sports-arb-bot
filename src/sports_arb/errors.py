"""Domain-specific exceptions."""


class SportsArbError(Exception):
    """Base exception for expected application failures."""


class MarketDataError(SportsArbError):
    """Raised when an upstream object is malformed or internally inconsistent."""


class ApiError(SportsArbError):
    """Raised when an HTTP API request fails."""
