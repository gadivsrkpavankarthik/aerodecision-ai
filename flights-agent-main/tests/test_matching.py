"""Unit tests for deterministic matching engine."""

import pytest
from models import FlightOption, FareResult
from matching import (
    normalize_flight_number,
    is_exact_flight_match,
    match_flights_with_fares,
)


def test_normalize_flight_number():
    assert normalize_flight_number("6E327") == "6E327"
    assert normalize_flight_number("6E-327") == "6E327"
    assert normalize_flight_number("6e 327") == "6E327"
    assert normalize_flight_number("6E / 327") == "6E327"
    assert normalize_flight_number("AI 865") == "AI865"


def test_critical_matching_exact_match():
    """Section 43 Critical Test 1: Exact Match."""
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        airline_code="6E",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time="08:10",
        arrival_time="10:20"
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        airline_code="6E",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time="08:10",
        arrival_time="10:20",
        price=6247.0,
        currency="INR",
        price_available=True,
        price_status="verified",
        ignav_id="ignav_exact_123"
    )

    assert is_exact_flight_match(flight, fare) is True
    matched = match_flights_with_fares([flight], [fare])
    assert len(matched) == 1
    assert matched[0].price == 6247.0
    assert matched[0].price_status == "verified"
    assert matched[0].ignav_id == "ignav_exact_123"
    assert matched[0].price_available is True


def test_critical_matching_wrong_flight_number():
    """Section 43 Critical Test 2: Wrong Flight Number."""
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E521",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=7100.0,
        price_status="verified"
    )

    assert is_exact_flight_match(flight, fare) is False
    matched = match_flights_with_fares([flight], [fare])
    assert len(matched) == 1
    assert matched[0].price is None
    assert matched[0].price_status == "unavailable"
    assert matched[0].price_available is False


def test_critical_matching_reversed_route():
    """Section 43 Critical Test 3: DEL->BOM vs BOM->DEL."""
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        origin="BOM",
        destination="DEL",
        travel_date="2026-09-24",
        price=6247.0,
        price_status="verified"
    )
    assert is_exact_flight_match(flight, fare) is False


def test_critical_matching_wrong_date():
    """Section 43 Critical Test 4: 2026-09-24 vs 2026-09-25."""
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6247.0,
        price_status="verified"
    )
    # Different date
    fare.travel_date = "2026-09-25"
    assert is_exact_flight_match(flight, fare) is False


def test_matching_wrong_airline():
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="Air India",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6247.0,
        price_status="verified"
    )
    assert is_exact_flight_match(flight, fare) is False


def test_matching_rejects_conflicting_carrier_codes_even_with_same_number():
    flight = FlightOption(
        flight_number="6E327",
        airline_code="6E",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        airline_code="AI",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6247.0,
    )

    assert not is_exact_flight_match(flight, fare)


def test_matching_stops_mismatch():
    """Nonstop flight cannot match 1-stop itinerary."""
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        stops=0
    )
    fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6247.0,
        stops=1
    )
    assert is_exact_flight_match(flight, fare) is False


@pytest.mark.parametrize(
    ("schedule_time", "fare_time", "expected"),
    [
        ("08:10", "08:25", True),
        ("08:10", "08:26", False),
        ("23:55", "00:05", True),
        ("invalid", "08:10", False),
    ],
)
def test_matching_validates_departure_time(schedule_time, fare_time, expected):
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time=schedule_time,
    )
    fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time=fare_time,
        price=6247.0,
    )

    assert is_exact_flight_match(flight, fare) is expected


def test_matching_preserves_unverified_status():
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6247.0,
        price_status="unverified"
    )
    matched = match_flights_with_fares([flight], [fare])
    assert len(matched) == 1
    assert matched[0].price_status == "unverified"
    assert matched[0].price == 6247.0


def test_matching_ignores_same_market_wrong_flight_number_fare():
    flight = FlightOption(
        flight_number="6E101",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time="08:15",
    )
    fare = FareResult(
        flight_number="6E909",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time="08:20",
        price=5500.0,
        currency="INR",
        price_available=True,
        price_status="verified",
    )

    matched = match_flights_with_fares([flight], [fare])
    assert len(matched) == 1
    assert matched[0].flight_number == "6E101"
    assert matched[0].schedule_matched is False
    assert matched[0].constraint_status == "missing_price"


def test_matching_returns_schedule_only_result_when_no_fare_exists():
    flight = FlightOption(
        flight_number="6E101",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )

    matched = match_flights_with_fares([flight], [])
    assert len(matched) == 1
    assert matched[0].schedule_matched is False
    assert matched[0].price is None
    assert matched[0].constraint_status == "missing_price"


def test_matching_handles_multiple_fares_for_one_schedule():
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        departure_time="08:10",
    )
    fares = [
        FareResult(
            flight_number="6E327",
            airline="IndiGo",
            origin="DEL",
            destination="BOM",
            travel_date="2026-09-24",
            departure_time="08:10",
            price=5500.0,
            currency="INR",
            price_available=True,
            price_status="verified",
        ),
        FareResult(
            flight_number="6E327",
            airline="IndiGo",
            origin="DEL",
            destination="BOM",
            travel_date="2026-09-24",
            departure_time="08:12",
            price=6100.0,
            currency="INR",
            price_available=True,
            price_status="verified",
        ),
    ]

    matched = match_flights_with_fares([flight], fares)
    assert len(matched) == 2
    assert sum(1 for item in matched if item.schedule_matched) == 2


def test_matching_deduplicates_duplicate_fare_records():
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6400.0,
        currency="INR",
        price_available=True,
        price_status="verified",
        ignav_id="dup-1",
    )

    matched = match_flights_with_fares([flight], [fare, fare])
    assert len(matched) == 1
    assert matched[0].ignav_id == "dup-1"


def test_matching_rejects_missing_flight_number():
    flight = FlightOption(
        flight_number="",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6400.0,
        price_available=True,
        price_status="verified",
    )
    assert not is_exact_flight_match(flight, fare)


def test_matching_rejects_different_flight_numbers():
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E521",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6400.0,
        price_available=True,
        price_status="verified",
    )
    assert not is_exact_flight_match(flight, fare)


def test_matching_rejects_airline_code_mismatch():
    flight = FlightOption(
        flight_number="6E327",
        airline_code="6E",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        airline_code="AI",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6400.0,
        price_available=True,
        price_status="verified",
    )
    assert not is_exact_flight_match(flight, fare)


def test_matching_rejects_route_mismatch():
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="CCU",
        travel_date="2026-09-24",
        price=6400.0,
        price_available=True,
        price_status="verified",
    )
    assert not is_exact_flight_match(flight, fare)


def test_matching_rejects_date_mismatch():
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-25",
        price=6400.0,
        price_available=True,
        price_status="verified",
    )
    assert not is_exact_flight_match(flight, fare)
