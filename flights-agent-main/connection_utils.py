"""Timezone-aware layover and connecting-itinerary helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from models import FareResult, ItineraryLeg

# Primary hubs used for domestic synthesis (IANA time zones)
AIRPORT_TIMEZONES: dict[str, str] = {
    "DEL": "Asia/Kolkata",
    "BOM": "Asia/Kolkata",
    "BLR": "Asia/Kolkata",
    "HYD": "Asia/Kolkata",
    "MAA": "Asia/Kolkata",
    "CCU": "Asia/Kolkata",
    "ATQ": "Asia/Kolkata",
    "VGA": "Asia/Kolkata",
    "GOI": "Asia/Kolkata",
    "COK": "Asia/Kolkata",
    "PNQ": "Asia/Kolkata",
    "JAI": "Asia/Kolkata",
    "LHR": "Europe/London",
    "JFK": "America/New_York",
    "DXB": "Asia/Dubai",
    "SIN": "Asia/Singapore",
}

MIN_LAYOVER_MINUTES = 60
MAX_LAYOVER_MINUTES = 720


def airport_timezone(iata: str) -> ZoneInfo:
    tz_name = AIRPORT_TIMEZONES.get(iata.upper())
    if not tz_name:
        raise ValueError(f"No configured timezone for airport {iata.upper()}.")
    return ZoneInfo(tz_name)


def _provider_datetime(raw: str, airport: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=airport_timezone(airport))
    return value


def oldest_timestamp(*values: Optional[str]) -> Optional[str]:
    parsed_values: list[datetime] = []
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed_values.append(parsed.astimezone(timezone.utc))
    return min(parsed_values).isoformat() if parsed_values else None


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    parts = time_str.strip()[:5].split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time: {time_str!r}")
    return int(parts[0]), int(parts[1])


def local_datetime_on_or_after(
    base: date,
    time_hhmm: str,
    tz: ZoneInfo,
    not_before: Optional[datetime] = None,
) -> datetime:
    """Build tz-aware local datetime on base date, rolling forward days if before not_before."""
    hour, minute = _parse_hhmm(time_hhmm)
    dt = datetime(base.year, base.month, base.day, hour, minute, tzinfo=tz)
    if not_before is not None:
        while dt < not_before:
            dt += timedelta(days=1)
    return dt


def infer_arrival_datetime(
    travel_date: str,
    departure_time: str,
    arrival_time: str,
    origin: str,
    destination: str,
) -> datetime:
    """Infer arrival instant using local times; rolls arrival to next day if before departure clock."""
    dep_date = date.fromisoformat(travel_date)
    dep_tz = airport_timezone(origin)
    arr_tz = airport_timezone(destination)
    dep_dt = local_datetime_on_or_after(dep_date, departure_time, dep_tz)
    arr_dt = local_datetime_on_or_after(dep_date, arrival_time, arr_tz)
    if arr_dt <= dep_dt.replace(tzinfo=dep_tz):
        arr_dt += timedelta(days=1)
    return arr_dt


@dataclass
class ConnectionValidation:
    ok: bool
    layover_minutes: Optional[int] = None
    leg2_departure: Optional[datetime] = None
    reason: Optional[str] = None


def validate_hub_connection(
    leg1: FareResult,
    leg2: FareResult,
    hub: str,
    travel_date: str,
    min_layover: int = MIN_LAYOVER_MINUTES,
    max_layover: int = MAX_LAYOVER_MINUTES,
) -> ConnectionValidation:
    """Validate two one-way fares connect at hub with acceptable layover (tz-aware)."""
    if leg1.destination.upper() != hub.upper() or leg2.origin.upper() != hub.upper():
        return ConnectionValidation(ok=False, reason="Hub airport mismatch between legs.")
    if leg1.travel_date != travel_date:
        return ConnectionValidation(ok=False, reason="First-leg date does not match the requested travel date.")

    if not leg1.departure_at or not leg1.arrival_at or not leg2.departure_at or not leg2.arrival_at:
        return ConnectionValidation(ok=False, reason="Missing full schedule timestamps for connection check.")

    try:
        leg1_dep = _provider_datetime(leg1.departure_at, leg1.origin)
        leg1_arr = _provider_datetime(leg1.arrival_at, leg1.destination)
        leg2_dep = _provider_datetime(leg2.departure_at, hub)
        leg2_arr = _provider_datetime(leg2.arrival_at or "", leg2.destination)
        if leg1_dep.astimezone(airport_timezone(leg1.origin)).date().isoformat() != leg1.travel_date:
            return ConnectionValidation(ok=False, reason="First-leg departure date conflicts with its fare record.")
        if leg2_dep.astimezone(airport_timezone(hub)).date().isoformat() != leg2.travel_date:
            return ConnectionValidation(ok=False, reason="Second-leg departure date conflicts with its fare record.")
        if leg1_arr.astimezone(timezone.utc) <= leg1_dep.astimezone(timezone.utc):
            return ConnectionValidation(ok=False, reason="First-leg arrival is not after departure.")
        if leg2_arr.astimezone(timezone.utc) <= leg2_dep.astimezone(timezone.utc):
            return ConnectionValidation(ok=False, reason="Second-leg arrival is not after departure.")
    except (ValueError, TypeError) as e:
        return ConnectionValidation(ok=False, reason=f"Invalid schedule times: {e}")

    layover = int((leg2_dep.astimezone(timezone.utc) - leg1_arr.astimezone(timezone.utc)).total_seconds() // 60)
    if layover < min_layover:
        return ConnectionValidation(ok=False, layover_minutes=layover, reason="Layover below minimum.")
    if layover > max_layover:
        return ConnectionValidation(ok=False, layover_minutes=layover, reason="Layover exceeds maximum.")

    return ConnectionValidation(ok=True, layover_minutes=layover, leg2_departure=leg2_dep)


def build_itinerary_legs(
    leg1: FareResult,
    leg2: FareResult,
    hub: str,
    travel_date: str,
    layover_minutes: int,
    leg2_departure: datetime,
) -> list[ItineraryLeg]:
    if not all((leg1.departure_at, leg1.arrival_at, leg2.departure_at, leg2.arrival_at)):
        raise ValueError("Full timestamps are required to build a connecting itinerary.")
    leg1_dep = _provider_datetime(leg1.departure_at, leg1.origin)
    leg1_arr = _provider_datetime(leg1.arrival_at, leg1.destination)
    leg2_dep = _provider_datetime(leg2.departure_at, leg2.origin)
    leg2_arr = _provider_datetime(leg2.arrival_at, leg2.destination)
    if leg2_dep.astimezone(timezone.utc) != leg2_departure.astimezone(timezone.utc):
        raise ValueError("Second-leg timestamp changed during connection validation.")

    return [
        ItineraryLeg(
            leg_index=1,
            flight_number=leg1.flight_number,
            airline=leg1.airline,
            origin=leg1.origin,
            destination=leg1.destination,
            travel_date=leg1_dep.astimezone(airport_timezone(leg1.origin)).date().isoformat(),
            departure_time=leg1.departure_time,
            arrival_time=leg1.arrival_time,
            departure_at=leg1_dep.isoformat(),
            arrival_at=leg1_arr.isoformat(),
            price=leg1.price,
            currency=leg1.currency,
            price_status=leg1.price_status,
            retrieved_at=leg1.retrieved_at,
            booking_url=leg1.booking_url,
            booking_provider=leg1.booking_provider,
            booking_link_kind=leg1.booking_link_kind,
            source=leg1.source,
            ignav_id=leg1.ignav_id,
        ),
        ItineraryLeg(
            leg_index=2,
            flight_number=leg2.flight_number,
            airline=leg2.airline,
            origin=leg2.origin,
            destination=leg2.destination,
            travel_date=leg2_dep.astimezone(airport_timezone(leg2.origin)).date().isoformat(),
            departure_time=leg2.departure_time,
            arrival_time=leg2.arrival_time,
            departure_at=leg2_departure.isoformat(),
            arrival_at=leg2_arr.isoformat(),
            price=leg2.price,
            currency=leg2.currency,
            price_status=leg2.price_status,
            retrieved_at=leg2.retrieved_at,
            booking_url=leg2.booking_url,
            booking_provider=leg2.booking_provider,
            booking_link_kind=leg2.booking_link_kind,
            source=leg2.source,
            ignav_id=leg2.ignav_id,
        ),
    ]


def combined_price_if_compatible(leg1: FareResult, leg2: FareResult) -> tuple[Optional[float], Optional[str], str]:
    """Sum leg prices only when both exist and currencies match."""
    if leg1.price is None or leg2.price is None:
        return None, None, "Missing fare on one or both legs."
    if leg1.price <= 0 or leg2.price <= 0:
        return None, None, "One or both leg prices are not positive values."
    if not leg1.currency or not leg2.currency:
        return None, None, "Currency missing on one or both legs."
    c1 = leg1.currency.upper()
    c2 = leg2.currency.upper()
    if c1 != c2:
        return None, None, f"Cannot sum fares in {c1} and {c2}."
    return leg1.price + leg2.price, c1, ""


def evaluate_transfer_risk(
    leg1: FareResult,
    leg2: FareResult,
    allow_unknown: bool = True,
) -> dict[str, object]:
    """Return a deterministic transfer-risk classification and explanation.

    The classification is intentionally descriptive and evidence-based, not a statistical
    missed-connection probability model.
    """
    if not leg1 or not leg2:
        return {
            "category": "INSUFFICIENT_INFORMATION",
            "explanation": "Missing transfer evidence.",
            "checklist": [
                "Confirm baggage collection requirements.",
                "Check whether a new boarding pass is required.",
                "Check with airline for transfer details.",
            ],
        }

    if not leg1.departure_at or not leg1.arrival_at or not leg2.departure_at or not leg2.arrival_at:
        return {
            "category": "INSUFFICIENT_INFORMATION",
            "explanation": "Transfer timing data is incomplete.",
            "checklist": [
                "Confirm baggage collection requirements.",
                "Check whether a new boarding pass is required.",
                "Check with airline for transfer details.",
            ],
        }

    if leg1.destination.upper() != leg2.origin.upper():
        return {
            "category": "HIGHER_CONCERN",
            "explanation": "Route continuity is invalid for a self-transfer connection.",
            "checklist": [
                "Confirm the second leg departs from the same airport where the first leg arrives.",
                "Check transfer terminal information.",
                "Review separate-ticket transfer conditions.",
            ],
        }

    validation = validate_hub_connection(leg1, leg2, leg1.destination, leg1.travel_date)
    layover_minutes = validation.layover_minutes or 0
    if not validation.ok:
        if validation.reason and "Missing" in validation.reason:
            return {
                "category": "INSUFFICIENT_INFORMATION",
                "explanation": validation.reason,
                "checklist": [
                    "Confirm baggage collection requirements.",
                    "Check whether a new boarding pass is required.",
                    "Check with airline for transfer details.",
                ],
            }
        return {
            "category": "HIGHER_CONCERN",
            "explanation": validation.reason or "Connection window is not suitable for a separate-ticket transfer.",
            "checklist": [
                "Confirm baggage collection requirements.",
                "Check whether a new boarding pass is required.",
                "Confirm terminal transfer and immigration requirements.",
            ],
        }

    if leg1.currency and leg2.currency and leg1.currency.upper() != leg2.currency.upper():
        return {
            "category": "MODERATE_CONCERN",
            "explanation": "Legs are priced in different currencies; transfer costs cannot be summed without a verified FX rate.",
            "checklist": [
                "Confirm baggage collection requirements.",
                "Check whether a new boarding pass is required.",
                "Review separate-ticket booking conditions.",
            ],
        }

    if layover_minutes < 120:
        return {
            "category": "HIGHER_CONCERN",
            "explanation": "The connection window is tight for a separate-ticket itinerary.",
            "checklist": [
                "Confirm baggage collection requirements.",
                "Check whether a new boarding pass is required.",
                "Check transfer terminal information.",
                "Review separate-ticket conditions.",
            ],
        }

    if layover_minutes > 720:
        return {
            "category": "MODERATE_CONCERN",
            "explanation": "Layover is long enough to be manageable but unusually extended for a single-day transfer.",
            "checklist": [
                "Confirm baggage collection requirements.",
                "Review the overnight transfer if applicable.",
            ],
        }

    if leg1.destination.upper() != leg2.origin.upper():
        return {
            "category": "HIGHER_CONCERN",
            "explanation": "Airport continuity is broken across legs.",
            "checklist": [
                "Confirm terminal information.",
                "Check airport transfer requirements.",
            ],
        }

    return {
        "category": "LOWER_CONCERN",
        "explanation": "No obvious transfer issue identified from the available schedule and fare data.",
        "checklist": [
            "Confirm baggage collection requirements.",
            "Check whether a new boarding pass is required.",
            "Review separate-ticket booking conditions.",
        ],
    }


def compute_trip_cost_breakdown(
    fares: list[FareResult] | FareResult,
    user_expenses: Optional[dict[str, float]] = None,
    passengers: int = 1,
) -> dict[str, object]:
    """Compute a structured trip cost breakdown for one or more fares.

    The total is only summed where currencies are compatible and the underlying pricing basis
    is not contradicted by missing data. Unknown fees are reported rather than silently set to zero.
    """
    if isinstance(fares, FareResult):
        fare_list = [fares]
    else:
        fare_list = list(fares or [])

    user_expenses = user_expenses or {}
    charged_fares = [fare for fare in fare_list if fare.price is not None]
    currencies = {fare.currency.upper() for fare in charged_fares if fare.currency}
    if len(currencies) > 1:
        combined_currency = None
        combined_total = None
        missing_info = ["Currency mismatch prevents summing fare components."]
    else:
        combined_currency = next(iter(currencies), None)
        combined_total = sum(float(fare.price) for fare in charged_fares if fare.price is not None)
        missing_info = []

    additional_known = sum(float(value) for value in user_expenses.values() if float(value) >= 0)
    total_estimate = combined_total + additional_known if combined_total is not None else None
    if total_estimate is not None:
        total_estimate *= max(1, passengers)

    return {
        "base_fare": combined_total,
        "currency": combined_currency,
        "known_additional_costs": additional_known,
        "estimated_total": total_estimate,
        "missing_information": missing_info,
        "per_passenger_cost": (combined_total / max(1, passengers)) if combined_total is not None else None,
        "passengers": passengers,
        "components": [
            {"kind": "fare", "amount": float(fare.price), "currency": fare.currency, "source": "provider"}
            for fare in charged_fares
        ],
        "user_entered_expenses": user_expenses,
    }
