"""Independent fare, schedule, seat, and booking verification.

A verified fare is never treated as proof of a verified schedule.
Missing provider evidence is labeled UNKNOWN or NOT CONFIRMED rather than invented.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config import config
from models import FareResult, FlightEvidence, IndependentVerification
from engines.status_mapping import candidate_status

VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
UNKNOWN = "UNKNOWN"
NOT_CONFIRMED = "NOT CONFIRMED"

_FRESHNESS_ORDER = {"FRESH": 0, "UNKNOWN": 1, "AGING": 2, "STALE": 3}


def _freshness(retrieved_at: str | None, max_age_seconds: float) -> tuple[str, float | None]:
    """Classify evidence freshness; missing timestamps are UNKNOWN, never fresh."""
    if not retrieved_at:
        return "UNKNOWN", None
    try:
        dt = datetime.fromisoformat(str(retrieved_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "UNKNOWN", None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    if age <= max_age_seconds:
        return "FRESH", age
    if age <= max_age_seconds * 2:
        return "AGING", age
    return "STALE", age


def _expires_at(facet_statuses: dict, facet_freshness: dict, now_iso: str) -> str | None:
    """Earliest expiry among VERIFIED facets; None when nothing is verified."""
    from datetime import timedelta
    thresholds = {
        "fare": config.verification_fare_max_age_hours * 3600,
        "schedule": config.verification_schedule_max_age_hours * 3600,
        "seats": config.verification_seats_max_age_hours * 3600,
        "booking": config.verification_booking_max_age_minutes * 60,
    }
    verified = [name for name, status in facet_statuses.items() if status == VERIFIED]
    if not verified:
        return None
    now = datetime.fromisoformat(now_iso)
    earliest = min(thresholds[name] for name in verified)
    return (now + timedelta(seconds=earliest)).isoformat()


def _booking_state(fare: FareResult) -> str:
    """Separate link existence/validity/reachability from actual bookability."""
    if not fare.booking_url and fare.web_verification_status in {None, "", "unavailable"}:
        return "NO_LINK"
    status = fare.web_verification_status
    if status == "passed":
        return "LINK_AVAILABLE"
    if status == "partial":
        return "BOOKING_DETAILS_UNCONFIRMED"
    if status == "failed":
        return "LINK_UNREACHABLE"
    return "BOOKABILITY_UNCONFIRMED"


def _fare_status(fare: FareResult) -> str:
    if fare.price_status == "verified" and fare.price is not None:
        return VERIFIED
    if fare.price_status == "unverified" and fare.price is not None:
        return UNVERIFIED
    if fare.price_status in {"unavailable", "error"} or fare.price is None:
        return NOT_CONFIRMED
    return UNKNOWN


def _schedule_status(fare: FareResult, schedule_provider_status: str | None = None) -> str:
    has_schedule_record = bool(fare.schedule_matched or fare.schedule_retrieved_at)
    if has_schedule_record:
        return VERIFIED
    if schedule_provider_status in {"error", "malformed_response", "provider_unavailable", "unavailable"}:
        return UNKNOWN
    if fare.price is not None or fare.price_available:
        return NOT_CONFIRMED
    return UNKNOWN


def _seat_status(fare: FareResult) -> str:
    if fare.availability_status == "provider_confirmed":
        return VERIFIED
    if fare.availability_status == "unavailable":
        return NOT_CONFIRMED
    return UNKNOWN


def _booking_status(fare: FareResult) -> str:
    mapping = {
        "passed": VERIFIED,
        "partial": UNVERIFIED,
        "failed": NOT_CONFIRMED,
        "unavailable": UNKNOWN,
    }
    return mapping.get(fare.web_verification_status, UNKNOWN)


def apply_verification(
    fare: FareResult,
    schedule_provider_status: str | None = None,
) -> FareResult:
    """Attach independent verification without collapsing fare and schedule evidence."""
    fare_status = _fare_status(fare)
    schedule_status = _schedule_status(fare, schedule_provider_status)
    seats_status = _seat_status(fare)
    booking_status = _booking_status(fare)
    fare.booking_state = _booking_state(fare)

    now = datetime.now(timezone.utc).isoformat()
    facet_freshness = {
        "fare": _freshness(fare.retrieved_at, config.verification_fare_max_age_hours * 3600),
        "schedule": _freshness(fare.schedule_retrieved_at, config.verification_schedule_max_age_hours * 3600),
        "seats": _freshness(fare.retrieved_at, config.verification_seats_max_age_hours * 3600),
        "booking": _freshness(fare.retrieved_at if fare.booking_url else None, config.verification_booking_max_age_minutes * 60),
    }
    facet_statuses = {"fare": fare_status, "schedule": schedule_status, "seats": seats_status, "booking": booking_status}

    notes: list[str] = []
    for facet, (freshness, _age) in facet_freshness.items():
        if facet_statuses[facet] == VERIFIED and freshness == "STALE":
            facet_statuses[facet] = UNVERIFIED
            notes.append(f"{facet.capitalize()} evidence expired; prior verification is no longer current.")
        elif facet_statuses[facet] == VERIFIED and freshness == "UNKNOWN":
            notes.append(f"{facet.capitalize()} verification lacks a retrieval timestamp; freshness cannot be established.")
        elif freshness == "STALE":
            notes.append(f"{facet.capitalize()} evidence is stale.")
    fare_status, schedule_status, seats_status, booking_status = (
        facet_statuses["fare"], facet_statuses["schedule"], facet_statuses["seats"], facet_statuses["booking"]
    )
    if fare_status == VERIFIED and schedule_status != VERIFIED:
        notes.append("A verified fare is not proof of a verified flight schedule.")
    if schedule_status == VERIFIED and fare_status in {NOT_CONFIRMED, UNKNOWN}:
        notes.append("Schedule evidence is independent of fare confirmation.")
    if seats_status != VERIFIED:
        notes.append("Seat availability is not confirmed by fare or schedule verification.")
    if booking_status == UNKNOWN:
        notes.append("No independent booking-page confirmation is available.")
    if schedule_provider_status in {"error", "malformed_response", "provider_unavailable"} and schedule_status == UNKNOWN:
        notes.append("Schedule provider did not return sufficient information.")

    fare.verification = IndependentVerification(
        fare=fare_status,
        schedule=schedule_status,
        seats=seats_status,
        booking=booking_status,
        fare_source=fare.fare_source or (fare.source if fare.price is not None else None),
        schedule_source=fare.schedule_source or ("AviationStack" if fare.schedule_matched or fare.schedule_retrieved_at else None),
        booking_source="booking_page" if fare.web_verification_url or fare.booking_url else None,
        fare_retrieved_at=fare.retrieved_at,
        schedule_retrieved_at=fare.schedule_retrieved_at,
        verified_at=now,
        last_checked_at=now,
        expires_at=_expires_at(facet_statuses, facet_freshness, now),
        evidence_age_seconds=max(
            (age for _f, (_s, age) in facet_freshness.items() if age is not None),
            default=None,
        ),
        freshness_status=max(
            (status for status, _age in facet_freshness.values()),
            key=lambda s: _FRESHNESS_ORDER[s],
            default="UNKNOWN",
        ),
        verification_source="provider_pipeline",
        notes=notes,
    )
    fare.trust_status = candidate_status(fare)
    fare.fare_source = fare.verification.fare_source
    fare.schedule_source = fare.verification.schedule_source

    if fare.price is None:
        fare.fare_status = "Schedule only" if fare.schedule_retrieved_at else "Unavailable"
    elif fare.schedule_matched:
        fare.fare_status = "Matched"
    else:
        fare.fare_status = "Fare only"

    if fare.schedule_matched or fare.schedule_retrieved_at:
        fare.schedule_status = "Matched" if fare.schedule_matched else "Schedule only"
    elif fare.price is not None:
        fare.schedule_status = "Fare only"
    else:
        fare.schedule_status = "Unknown"

    fare.evidence = FlightEvidence(
        fare_source=fare.fare_source,
        fare_retrieved_at=fare.retrieved_at,
        fare_status=fare.fare_status,
        schedule_source=fare.schedule_source,
        schedule_retrieved_at=fare.schedule_retrieved_at,
        schedule_status=fare.schedule_status,
        schedule_matched=fare.schedule_matched,
        booking_url=fare.booking_url,
        booking_verification_status=(
            fare.web_verification_status if fare.web_verification_status in {"passed", "partial", "unavailable", "failed"} else "unknown"
        ),
        availability_status=fare.availability_status,
        evidence_messages=list(notes),
        overall_data_freshness=fare.overall_data_freshness,
    )
    fare.evidence_messages = list(notes)
    return fare


def apply_verification_to_many(
    fares: list[FareResult],
    schedule_provider_status: str | None = None,
) -> list[FareResult]:
    for fare in fares:
        apply_verification(fare, schedule_provider_status=schedule_provider_status)
    return fares
