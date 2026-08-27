"""Typed failures raised by deterministic technical analytics."""

from enum import StrEnum
from typing import Any


class TechnicalAnalyticsErrorCode(StrEnum):
    EMPTY_SERIES = "EMPTY_SERIES"
    INVALID_ROW = "INVALID_ROW"
    DUPLICATE_DATE = "DUPLICATE_DATE"
    MISSING_ADJUSTMENT_FACTOR = "MISSING_ADJUSTMENT_FACTOR"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_OVERLAPPING_DATES = "NO_OVERLAPPING_DATES"


class TechnicalAnalyticsError(ValueError):
    """A deterministic, caller-actionable analytics failure."""

    def __init__(
        self,
        code: TechnicalAnalyticsErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
