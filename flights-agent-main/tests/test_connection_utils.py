from agent import FlightOptionFinderAgent
from connection_utils import build_itinerary_legs, combined_price_if_compatible, validate_hub_connection
from memory import SessionMemory
from models import FareResult
from unittest.mock import patch


def _fare(
    origin: str,
    destination: str,
    travel_date: str,
    departure_at: str,
    arrival_at: str,
    flight_number: str,
    currency: str = "INR",
) -> FareResult:
    return FareResult(
        flight_number=flight_number,
        origin=origin,
        destination=destination,
        travel_date=travel_date,
        departure_time=departure_at[11:16],
        arrival_time=arrival_at[11:16],
        departure_at=departure_at,
        arrival_at=arrival_at,
        passengers=1,
        cabin_class="economy",
        price=1000,
        currency=currency,
        price_available=True,
        price_status="unverified",
    )


def test_connection_handles_midnight_and_overnight_layover():
    first = _fare("DEL", "BOM", "2026-11-01", "2026-11-01T22:00:00+05:30", "2026-11-02T00:30:00+05:30", "6E101")
    second = _fare("BOM", "MAA", "2026-11-02", "2026-11-02T03:00:00+05:30", "2026-11-02T06:00:00+05:30", "6E202")

    result = validate_hub_connection(first, second, "BOM", "2026-11-01")

    assert result.ok
    assert result.layover_minutes == 150
    assert result.leg2_departure.isoformat() == second.departure_at
    legs = build_itinerary_legs(first, second, "BOM", "2026-11-01", result.layover_minutes, result.leg2_departure)
    assert legs[1].travel_date == "2026-11-02"


def test_connection_uses_instants_across_timezones():
    first = _fare("LHR", "DEL", "2026-11-01", "2026-11-01T18:00:00+00:00", "2026-11-02T07:00:00+05:30", "AI101")
    second = _fare("DEL", "BOM", "2026-11-02", "2026-11-02T08:30:00+05:30", "2026-11-02T10:30:00+05:30", "AI202")

    result = validate_hub_connection(first, second, "DEL", "2026-11-01")

    assert result.ok
    assert result.layover_minutes == 90


def test_connection_rejects_missing_timestamps():
    first = _fare("DEL", "BOM", "2026-11-01", "2026-11-01T07:00:00+05:30", "2026-11-01T09:00:00+05:30", "6E101")
    second = _fare("BOM", "MAA", "2026-11-01", "2026-11-01T11:00:00+05:30", "2026-11-01T13:00:00+05:30", "6E202")
    second.departure_at = None

    result = validate_hub_connection(first, second, "BOM", "2026-11-01")

    assert not result.ok
    assert "Missing full schedule timestamps" in result.reason


def test_connection_rejects_too_short_and_too_long_layovers():
    first = _fare("DEL", "BOM", "2026-11-01", "2026-11-01T07:00:00+05:30", "2026-11-01T09:00:00+05:30", "6E101")
    short = _fare("BOM", "MAA", "2026-11-01", "2026-11-01T09:30:00+05:30", "2026-11-01T11:30:00+05:30", "6E202")
    long = _fare("BOM", "MAA", "2026-11-01", "2026-11-01T22:30:00+05:30", "2026-11-02T00:30:00+05:30", "6E202")

    assert not validate_hub_connection(first, short, "BOM", "2026-11-01").ok
    assert not validate_hub_connection(first, long, "BOM", "2026-11-01").ok


def test_connection_rejects_wrong_order_and_currency_mismatch():
    first = _fare("DEL", "BOM", "2026-11-01", "2026-11-01T07:00:00+05:30", "2026-11-01T09:00:00+05:30", "6E101")
    second = _fare("BOM", "MAA", "2026-11-01", "2026-11-01T08:30:00+05:30", "2026-11-01T11:00:00+05:30", "6E202", "USD")

    assert not validate_hub_connection(first, second, "BOM", "2026-11-01").ok
    assert combined_price_if_compatible(first, second)[0] is None


def test_connection_rejects_airport_change_and_nonchronological_leg():
    first = _fare("DEL", "BOM", "2026-11-01", "2026-11-01T07:00:00+05:30", "2026-11-01T09:00:00+05:30", "6E101")
    wrong_airport = _fare("MAA", "BOM", "2026-11-01", "2026-11-01T11:00:00+05:30", "2026-11-01T13:00:00+05:30", "6E202")
    invalid_arrival = _fare("BOM", "MAA", "2026-11-01", "2026-11-01T11:00:00+05:30", "2026-11-01T10:00:00+05:30", "6E202")

    assert not validate_hub_connection(first, wrong_airport, "BOM", "2026-11-01").ok
    result = validate_hub_connection(first, invalid_arrival, "BOM", "2026-11-01")
    assert not result.ok
    assert "Second-leg arrival is not after departure" in result.reason


def test_connection_combines_only_compatible_currencies():
    first = _fare("DEL", "BOM", "2026-11-01", "2026-11-01T07:00:00+05:30", "2026-11-01T09:00:00+05:30", "6E101")
    second = _fare("BOM", "MAA", "2026-11-01", "2026-11-01T11:00:00+05:30", "2026-11-01T13:00:00+05:30", "6E202")

    assert combined_price_if_compatible(first, second) == (2000, "INR", "")
    second.currency = "USD"
    assert combined_price_if_compatible(first, second)[0] is None


def test_connection_rejects_single_leg_only_results():
    first_leg = _fare("MAA", "DEL", "2026-11-01", "2026-11-01T07:00:00+05:30", "2026-11-01T09:00:00+05:30", "6E101")
    agent = FlightOptionFinderAgent(memory=SessionMemory())

    def search(origin, destination, **kwargs):
        if origin == "MAA" and destination == "DEL":
            return [first_leg]
        return []

    with patch("agent.ignav_client.search_one_way_fares", side_effect=search):
        result = agent._search_connecting_hubs("MAA", "COK", "2026-11-01")

    assert result == []


def test_self_transfer_synthesis_rejects_legs_that_already_have_stops():
    first_leg = _fare(
        "MAA",
        "DEL",
        "2026-11-01",
        "2026-11-01T07:00:00+05:30",
        "2026-11-01T09:00:00+05:30",
        "6E101",
    )
    first_leg.stops = 1
    second_leg = _fare(
        "DEL",
        "COK",
        "2026-11-01",
        "2026-11-01T11:00:00+05:30",
        "2026-11-01T13:00:00+05:30",
        "6E202",
    )

    def search(origin, destination, **kwargs):
        if origin == "MAA" and destination == "DEL":
            return [first_leg]
        if origin == "DEL" and destination == "COK":
            return [second_leg]
        return []

    agent = FlightOptionFinderAgent(memory=SessionMemory())
    with patch("agent.ignav_client.search_one_way_fares", side_effect=search):
        result = agent._search_connecting_hubs("MAA", "COK", "2026-11-01")

    assert result == []