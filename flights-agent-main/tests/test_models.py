"""Unit tests for domain models."""

import pytest
from models import FlightOption, FareResult, SearchRequest, BookingOption, ScrapingVerificationResult


def test_flight_option_creation():
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
    assert flight.flight_number == "6E327"
    assert flight.source == "AviationStack"
    assert flight.stops == 0


def test_fare_result_defaults_and_status():
    fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
        price=6247.0,
        currency="INR",
        price_available=True,
        price_status="verified",
        ignav_id="ignav_test_123"
    )
    assert fare.price == 6247.0
    assert fare.price_status == "verified"
    assert fare.source == "Ignav"
    assert fare.web_verification_status == "unavailable"


def test_search_request_defaults(sample_travel_date):
    req = SearchRequest(
        travel_date=sample_travel_date,
        origin_city="Delhi",
        destination_city="Mumbai",
        budget=8000.0
    )
    assert req.currency == "INR"
    assert req.passengers == 1
    assert req.cabin_class == "economy"
    assert req.budget == 8000.0


def test_search_request_validates_currency_stops_and_distinct_route(sample_travel_date):
    with pytest.raises(ValueError, match="Unsupported currency"):
        SearchRequest(travel_date=sample_travel_date, currency="JPY")

    with pytest.raises(ValueError, match="max_stops must be between 0 and 3"):
        SearchRequest(travel_date=sample_travel_date, max_stops=4)

    with pytest.raises(ValueError, match="Origin and destination must differ"):
        SearchRequest(
            travel_date=sample_travel_date,
            origin_code="DEL",
            destination_code="DEL",
        )


def test_search_request_rejects_non_finite_budget(sample_travel_date):
    with pytest.raises(ValueError, match="finite number"):
        SearchRequest(travel_date=sample_travel_date, budget=float("inf"))
