"""Deterministic Matching Engine for Flight Schedules and Fares.

Compares AviationStack FlightOption records with Ignav FareResult records
under strict deterministic rules without LLM intervention.
"""

import re
import logging
from typing import Optional
from models import FlightOption, FareResult
from engines.verification_engine import apply_verification

logger = logging.getLogger(__name__)


def normalize_flight_number(raw: Optional[str]) -> str:
    """Normalize flight number into standard uppercase alphanumeric format (e.g. '6E-327' -> '6E327')."""
    if not raw:
        return ""
    # Strip spaces, dashes, slashes, dots
    cleaned = re.sub(r"[\s\-\/\.]", "", str(raw)).strip().upper()
    return cleaned


def extract_carrier_and_number(normalized: str) -> tuple[str, str]:
    """Extract 2-character carrier code and numeric flight number if present."""
    match = re.match(r"^([A-Z0-9]{2})(\d+)$", normalized)
    if match:
        return match.group(1), match.group(2)
    return "", normalized


def _normalize_airline_name(raw: Optional[str]) -> str:
    """Normalize airline names for equality checks while preserving provenance."""
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def _stable_fare_key(fare: FareResult) -> tuple[str, ...]:
    """Create a stable deduplication key for fare records without merging distinct flights."""
    flight_number = normalize_flight_number(fare.flight_number)
    carrier_code = (fare.airline_code or extract_carrier_and_number(flight_number)[0]).upper()
    return (
        fare.origin.upper(),
        fare.destination.upper(),
        fare.travel_date,
        carrier_code,
        flight_number,
        (fare.departure_time or "")[:5],
        fare.ignav_id or "",
        fare.booking_url or "",
        str(fare.price) if fare.price is not None else "",
    )


def is_exact_flight_match(flight: FlightOption, fare: FareResult) -> bool:
    """Deterministic check if an AviationStack flight and an Ignav fare record represent the exact same flight."""
    if flight.origin.upper() != fare.origin.upper():
        return False
    if flight.destination.upper() != fare.destination.upper():
        return False
    if flight.travel_date != fare.travel_date:
        return False

    flight_norm = normalize_flight_number(flight.flight_number)
    fare_norm = normalize_flight_number(fare.flight_number)
    if not flight_norm or not fare_norm:
        return False

    f_carrier, f_num = extract_carrier_and_number(flight_norm)
    fare_carrier, fare_num = extract_carrier_and_number(fare_norm)
    flight_code = (flight.airline_code or f_carrier).upper()
    fare_code = (fare.airline_code or fare_carrier).upper()

    if flight_code and fare_code and flight_code != fare_code:
        return False
    if f_carrier and fare_carrier and f_carrier != fare_carrier:
        return False
    if flight_norm != fare_norm and not (f_num and fare_num and f_num == fare_num):
        return False

    if flight.airline and fare.airline:
        a1 = _normalize_airline_name(flight.airline)
        a2 = _normalize_airline_name(fare.airline)
        if a1 != a2 and a1 not in a2 and a2 not in a1:
            return False

    if flight.stops != fare.stops:
        return False

    if flight.departure_time and fare.departure_time:
        t1 = flight.departure_time[:5]
        t2 = fare.departure_time[:5]
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t1):
            return False
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t2):
            return False
        m1 = int(t1[:2]) * 60 + int(t1[3:])
        m2 = int(t2[:2]) * 60 + int(t2[3:])
        difference = abs(m1 - m2)
        if min(difference, 24 * 60 - difference) > 15:
            return False

    return True


def _is_same_market_and_airline(flight: FlightOption, fare: FareResult) -> bool:
    """True when a schedule and fare belong to the same route/date/carrier and should not emit as a separate fare-only result."""
    if flight.origin.upper() != fare.origin.upper():
        return False
    if flight.destination.upper() != fare.destination.upper():
        return False
    if flight.travel_date != fare.travel_date:
        return False

    flight_norm = normalize_flight_number(flight.flight_number)
    fare_norm = normalize_flight_number(fare.flight_number)
    flight_code = (flight.airline_code or extract_carrier_and_number(flight_norm)[0]).upper()
    fare_code = (fare.airline_code or extract_carrier_and_number(fare_norm)[0]).upper()

    if flight_code and fare_code and flight_code != fare_code:
        return False

    if flight.airline and fare.airline:
        a1 = _normalize_airline_name(flight.airline)
        a2 = _normalize_airline_name(fare.airline)
        if a1 != a2 and a1 not in a2 and a2 not in a1:
            return False

    return bool(flight_code or flight.airline or fare.airline)


def _matched_fields(flight: FlightOption, fare: FareResult) -> list[str]:
    """Record which deterministic fields agreed for an exact match."""
    fields: list[str] = []
    if flight.origin.upper() == fare.origin.upper():
        fields.append("origin")
    if flight.destination.upper() == fare.destination.upper():
        fields.append("destination")
    if flight.travel_date == fare.travel_date:
        fields.append("travel_date")
    flight_norm = normalize_flight_number(flight.flight_number)
    fare_norm = normalize_flight_number(fare.flight_number)
    if flight_norm and fare_norm:
        fields.append("flight_number")
    flight_code = (flight.airline_code or extract_carrier_and_number(flight_norm)[0]).upper()
    fare_code = (fare.airline_code or extract_carrier_and_number(fare_norm)[0]).upper()
    if flight_code and fare_code and flight_code == fare_code:
        fields.append("carrier_code")
    if flight.airline and fare.airline:
        a1 = _normalize_airline_name(flight.airline)
        a2 = _normalize_airline_name(fare.airline)
        if a1 == a2 or a1 in a2 or a2 in a1:
            fields.append("airline")
    if flight.stops == fare.stops:
        fields.append("stops")
    if flight.departure_time and fare.departure_time:
        fields.append("departure_time")
    return fields


def classify_match(flight: FlightOption, fare: FareResult) -> tuple[str, list[str]]:
    """Classify a schedule-fare pair without inventing associations.

    EXACT_MATCH requires identical normalized flight numbers and carrier codes.
    HIGH_CONFIDENCE_MATCH allows equivalent numeric flight numbers with
    different formatting. PARTIAL_MATCH means same market/airline evidence but
    the exact-match gate failed. Anything else is NO_MATCH.
    """
    if not is_exact_flight_match(flight, fare):
        if _is_same_market_and_airline(flight, fare):
            return "PARTIAL_MATCH", _matched_fields(flight, fare)
        return "NO_MATCH", []
    flight_norm = normalize_flight_number(flight.flight_number)
    fare_norm = normalize_flight_number(fare.flight_number)
    flight_code = (flight.airline_code or extract_carrier_and_number(flight_norm)[0]).upper()
    fare_code = (fare.airline_code or extract_carrier_and_number(fare_norm)[0]).upper()
    confidence = (
        "EXACT_MATCH"
        if flight_norm == fare_norm and (not flight_code or not fare_code or flight_code == fare_code)
        else "HIGH_CONFIDENCE_MATCH"
    )
    return confidence, _matched_fields(flight, fare)


def _flight_dedup_key(flight: FlightOption) -> tuple:
    return (
        flight.origin.upper(),
        flight.destination.upper(),
        flight.travel_date,
        normalize_flight_number(flight.flight_number),
        (flight.departure_time or "")[:5],
    )


def match_flights_with_fares(
    flights: list[FlightOption],
    fares: list[FareResult],
) -> list[FareResult]:
    """Match schedules and fares while preserving fare-only and schedule-only cases.

    A result is preserved even when the schedule exists without a fare, or when the fare exists
    without a matching schedule. Duplicate schedule and fare records are deduplicated with
    stable keys. Ambiguous matches (one schedule matching multiple fares or vice versa) are
    not merged into a verified association.
    """
    # Deduplicate provider schedule records before matching.
    unique_flights: list[FlightOption] = []
    seen_flight_keys: set[tuple] = set()
    for flight in flights:
        key = _flight_dedup_key(flight)
        if key in seen_flight_keys:
            continue
        seen_flight_keys.add(key)
        unique_flights.append(flight)
    flights = unique_flights

    # Precompute exact-match cardinality so ambiguous pairs are never merged.
    flight_match_counts: dict[int, int] = {}
    fare_match_counts: dict[int, int] = {}
    exact_pairs: set[tuple[int, int]] = set()
    for fi, flight in enumerate(flights):
        for ri, fare in enumerate(fares):
            if is_exact_flight_match(flight, fare):
                exact_pairs.add((fi, ri))
                flight_match_counts[fi] = flight_match_counts.get(fi, 0) + 1
                fare_match_counts[ri] = fare_match_counts.get(ri, 0) + 1

    results: list[FareResult] = []
    seen_keys: set[tuple[str, ...]] = set()

    for fi, flight in enumerate(flights):
        matched_fares = [
            (ri, fares[ri])
            for ri in range(len(fares))
            if (fi, ri) in exact_pairs and fare_match_counts.get(ri, 0) == 1
        ]
        if matched_fares:
            for ri, fare in matched_fares:
                confidence, match_fields = classify_match(flight, fare)
                key = ("fare_match",) + _stable_fare_key(fare)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(
                    apply_verification(
                        FareResult(
                            flight_number=flight.flight_number,
                            association_level=fare.association_level,
                            provider_fields=dict(fare.provider_fields or {}),
                            airline=flight.airline or fare.airline,
                            airline_code=flight.airline_code or fare.airline_code,
                            origin=flight.origin,
                            destination=flight.destination,
                            travel_date=flight.travel_date,
                            departure_time=flight.departure_time or fare.departure_time,
                            arrival_time=flight.arrival_time or fare.arrival_time,
                            departure_at=fare.departure_at,
                            arrival_at=fare.arrival_at,
                            price=fare.price,
                            currency=fare.currency,
                            price_available=fare.price is not None and fare.price_available,
                            price_status=fare.price_status,
                            availability_status=fare.availability_status,
                            ignav_id=fare.ignav_id,
                            booking_url=fare.booking_url,
                            booking_provider=fare.booking_provider,
                            booking_provider_type=fare.booking_provider_type,
                            booking_link_kind=fare.booking_link_kind,
                            source=fare.source,
                            fare_source=fare.fare_source or fare.source,
                            schedule_source="AviationStack",
                            retrieved_at=fare.retrieved_at,
                            schedule_retrieved_at=flight.retrieved_at,
                            web_verification_status="unavailable",
                            stops=flight.stops,
                            passengers=fare.passengers,
                            cabin_class=fare.cabin_class,
                            schedule_matched=True,
                            match_confidence=confidence,
                            match_fields=match_fields,
                            constraint_status=None,
                            constraint_reason=None,
                        )
                    )
                )
        else:
            schedule_key = (
                "schedule_only",
                flight.origin.upper(),
                flight.destination.upper(),
                flight.travel_date,
                normalize_flight_number(flight.flight_number),
                (flight.departure_time or "")[:5],
            )
            if schedule_key in seen_keys:
                continue
            seen_keys.add(schedule_key)
            results.append(
                apply_verification(
                    FareResult(
                        flight_number=flight.flight_number,
                        airline=flight.airline,
                        airline_code=flight.airline_code,
                        origin=flight.origin,
                        destination=flight.destination,
                        travel_date=flight.travel_date,
                        departure_time=flight.departure_time,
                        arrival_time=flight.arrival_time,
                        price=None,
                        currency=None,
                        price_available=False,
                        price_status="unavailable",
                        availability_status="unavailable",
                        ignav_id=None,
                        booking_url=None,
                        booking_provider=None,
                        booking_provider_type=None,
                        source="AviationStack",
                        schedule_source="AviationStack",
                        schedule_retrieved_at=flight.retrieved_at,
                        web_verification_status="unavailable",
                        stops=flight.stops,
                        schedule_matched=False,
                        match_confidence="NO_MATCH",
                        match_fields=[],
                        constraint_status="missing_price",
                        constraint_reason="No matching Ignav fare for this schedule.",
                    )
                )
            )

    for ri, fare in enumerate(fares):
        fare_key = ("fare_only",) + _stable_fare_key(fare)
        if fare_key in seen_keys:
            continue
        # Fares merged into an unambiguous schedule match are already emitted.
        if fare_match_counts.get(ri, 0) == 1 and any(
            is_exact_flight_match(flight, fare) for flight in flights
        ):
            continue
        matching_schedule = next((flight for flight in flights if _is_same_market_and_airline(flight, fare)), None)
        if matching_schedule is not None and fare_match_counts.get(ri, 0) == 0:
            continue
        ambiguous = fare_match_counts.get(ri, 0) > 1
        confidence, match_fields = (
            ("PARTIAL_MATCH", _matched_fields(matching_schedule, fare))
            if matching_schedule is not None
            else ("NO_MATCH", [])
        )
        seen_keys.add(fare_key)
        results.append(
            apply_verification(
                FareResult(
                    flight_number=fare.flight_number,
                    association_level=fare.association_level,
                    provider_fields=dict(fare.provider_fields or {}),
                    airline=fare.airline,
                    airline_code=fare.airline_code,
                    origin=fare.origin,
                    destination=fare.destination,
                    travel_date=fare.travel_date,
                    departure_time=fare.departure_time,
                    arrival_time=fare.arrival_time,
                    departure_at=fare.departure_at,
                    arrival_at=fare.arrival_at,
                    price=fare.price,
                    currency=fare.currency,
                    price_available=fare.price is not None and fare.price_available,
                    price_status=fare.price_status,
                    availability_status=fare.availability_status,
                    ignav_id=fare.ignav_id,
                    booking_url=fare.booking_url,
                    booking_provider=fare.booking_provider,
                    booking_provider_type=fare.booking_provider_type,
                    booking_link_kind=fare.booking_link_kind,
                    source=fare.source,
                    fare_source=fare.fare_source or fare.source,
                    retrieved_at=fare.retrieved_at,
                    schedule_retrieved_at=None,
                    web_verification_status="unavailable",
                    stops=fare.stops,
                    passengers=fare.passengers,
                    cabin_class=fare.cabin_class,
                    schedule_matched=False,
                    match_confidence=confidence,
                    match_fields=match_fields,
                    constraint_status="missing_schedule",
                    constraint_reason=(
                        "Ambiguous schedule match; fare kept separate from schedule evidence."
                        if ambiguous
                        else "No matching AviationStack schedule for this fare."
                    ),
                )
            )
        )

    return results
