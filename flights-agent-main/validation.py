"""Shared deterministic validation for flight search inputs."""

from __future__ import annotations

import re
import math
from datetime import date, datetime, timezone
from typing import Optional

IATA_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_CABIN = frozenset({"economy", "premium_economy", "business", "first"})
ALLOWED_CURRENCIES = frozenset({"INR", "USD", "EUR", "GBP", "AED", "SGD", "AUD", "CAD"})


class ValidationError(ValueError):
    """Raised when user or API input fails deterministic validation."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


def normalize_iata(code: Optional[str]) -> str:
    if not code or not str(code).strip():
        raise ValidationError("IATA airport code is required.", field="airport")
    normalized = str(code).strip().upper()
    if not IATA_CODE_PATTERN.match(normalized):
        raise ValidationError(f"Invalid IATA code: {code!r}", field="airport")
    return normalized


def parse_iso_travel_date(value: str) -> date:
    if not value or not ISO_DATE_PATTERN.match(value.strip()):
        raise ValidationError("Travel date must be YYYY-MM-DD.", field="travel_date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as e:
        raise ValidationError("Travel date is not a valid calendar date.", field="travel_date") from e


def validate_travel_date_not_past(value: str, *, allow_past: bool = False) -> str:
    """Validate ISO travel date; reject dates before today (UTC) for new searches."""
    parsed = parse_iso_travel_date(value)
    if not allow_past:
        today = datetime.now(timezone.utc).date()
        if parsed < today:
            raise ValidationError(
                f"Travel date {value} is in the past. Use today or a future date.",
                field="travel_date",
            )
    return parsed.isoformat()


def validate_passengers(count: int) -> int:
    try:
        n = int(count)
    except (TypeError, ValueError) as e:
        raise ValidationError("Passengers must be a positive integer.", field="passengers") from e
    if n < 1 or n > 9:
        raise ValidationError("Passengers must be between 1 and 9.", field="passengers")
    return n


def validate_budget(amount: Optional[float]) -> Optional[float]:
    if amount is None:
        return None
    try:
        val = float(amount)
    except (TypeError, ValueError) as e:
        raise ValidationError("Budget must be a number.", field="budget") from e
    if not math.isfinite(val):
        raise ValidationError("Budget must be a finite number.", field="budget")
    if val <= 0:
        raise ValidationError("Budget must be greater than zero.", field="budget")
    return val


def validate_currency(code: str) -> str:
    if not code or not str(code).strip():
        raise ValidationError("Currency code is required.", field="currency")
    normalized = str(code).strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValidationError(f"Invalid currency code: {code!r}", field="currency")
    if normalized not in ALLOWED_CURRENCIES:
        raise ValidationError(
            f"Unsupported currency: {normalized}. Supported currencies: {', '.join(sorted(ALLOWED_CURRENCIES))}.",
            field="currency",
        )
    return normalized


def validate_cabin_class(cabin: str) -> str:
    normalized = (cabin or "economy").strip().lower()
    if normalized not in ALLOWED_CABIN:
        raise ValidationError(
            f"Unsupported cabin class: {cabin!r}. Use one of {sorted(ALLOWED_CABIN)}.",
            field="cabin_class",
        )
    return normalized


def validate_max_stops(max_stops: Optional[int]) -> Optional[int]:
    if max_stops is None:
        return None
    if isinstance(max_stops, bool) or (
        isinstance(max_stops, float) and not max_stops.is_integer()
    ):
        raise ValidationError("max_stops must be an integer.", field="max_stops")
    try:
        n = int(max_stops)
    except (TypeError, ValueError) as e:
        raise ValidationError("max_stops must be an integer.", field="max_stops") from e
    if n < 0 or n > 3:
        raise ValidationError("max_stops must be between 0 and 3.", field="max_stops")
    return n
