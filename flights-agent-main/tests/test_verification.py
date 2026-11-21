"""Independent verification: fare confirmation never proves a schedule."""

from engines.verification_engine import apply_verification
from matching import match_flights_with_fares
from models import FareResult, FlightOption


def test_verified_fare_is_not_a_verified_schedule(sample_travel_date):
    fare = FareResult(
        flight_number="AI101",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=5400,
        currency="INR",
        price_available=True,
        price_status="verified",
        schedule_matched=False,
        source="Ignav",
    )
    apply_verification(fare, schedule_provider_status="success")

    assert fare.verification.fare == "VERIFIED"
    assert fare.verification.schedule == "NOT CONFIRMED"
    assert fare.verification.seats == "UNKNOWN"
    assert fare.verification.booking == "UNKNOWN"
    assert any("not proof of a verified flight schedule" in note.lower() for note in fare.verification.notes)


def test_schedule_only_does_not_invent_a_fare(sample_travel_date):
    flight = FlightOption(
        flight_number="6E101",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
    )
    matched = match_flights_with_fares([flight], [])
    assert len(matched) == 1
    assert matched[0].verification.schedule == "VERIFIED"
    assert matched[0].verification.fare == "NOT CONFIRMED"
    assert matched[0].price is None
    assert matched[0].trust_status["overall"] == "SCHEDULE ONLY"


def test_matched_fare_and_schedule_remain_independent(sample_travel_date):
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
        price=6247.0,
        currency="INR",
        price_available=True,
        price_status="verified",
    )
    matched = match_flights_with_fares([flight], [fare])
    assert matched[0].verification.fare == "VERIFIED"
    assert matched[0].verification.schedule == "VERIFIED"
    assert matched[0].verification.seats == "UNKNOWN"
    assert matched[0].schedule_matched is True


def test_schedule_provider_failure_is_unknown_not_unverified(sample_travel_date):
    fare = FareResult(
        flight_number="AI101",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=5000,
        currency="INR",
        price_available=True,
        price_status="verified",
        schedule_matched=False,
    )
    apply_verification(fare, schedule_provider_status="error")
    assert fare.verification.fare == "VERIFIED"
    assert fare.verification.schedule == "UNKNOWN"
    assert "did not return sufficient information" in " ".join(fare.verification.notes)


def test_unverified_fare_does_not_become_verified_after_schedule_match(sample_travel_date):
    flight = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
        price=6247.0,
        currency="INR",
        price_available=True,
        price_status="unverified",
    )
    matched = match_flights_with_fares([flight], [fare])
    assert matched[0].verification.schedule == "VERIFIED"
    assert matched[0].verification.fare == "UNVERIFIED"
    assert matched[0].price_status == "unverified"
