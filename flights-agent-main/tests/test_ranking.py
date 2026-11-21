from config import config
from models import FareResult, SearchRequest
from ranking import rank_fares


def _fare(number, price, *, airline=None, departure=None, status="verified", currency="INR"):
    return FareResult(
        flight_number=number,
        airline=airline,
        origin="DEL",
        destination="BOM",
        travel_date="2099-01-01",
        departure_time=departure,
        price=price,
        currency=currency,
        price_available=True,
        price_status=status,
    )


def _request(sample_travel_date, **kwargs):
    return SearchRequest(
        origin_code="DEL",
        destination_code="BOM",
        travel_date=sample_travel_date,
        **kwargs,
    )


def test_lowest_fare_wins_without_preferences(sample_travel_date):
    candidates = [_fare("6E200", 8000), _fare("AI100", 5000)]

    ranked = rank_fares(candidates, _request(sample_travel_date))

    assert ranked[0].flight_number == "AI100"


def test_ranking_uses_normalized_comparison_amount_across_currencies(sample_travel_date):
    expensive_native=_fare('AI100',100,currency='USD')
    cheap_native=_fare('6E200',5000,currency='INR')
    expensive_native.comparison_amount='900.00'; expensive_native.comparison_currency='INR'
    cheap_native.comparison_amount='5000.00'; cheap_native.comparison_currency='INR'
    ranked=rank_fares([cheap_native,expensive_native],_request(sample_travel_date,recommendation_profile='CHEAPEST'))
    assert ranked[0].flight_number=='AI100'


def test_preferred_airline_can_outweigh_price(sample_travel_date, monkeypatch):
    monkeypatch.setattr(config, "ranking_price_weight", 0.3)
    monkeypatch.setattr(config, "ranking_airline_weight", 0.5)
    monkeypatch.setattr(config, "ranking_departure_weight", 0.2)
    candidates = [_fare("AI100", 5000, airline="Air India"), _fare("6E200", 9000, airline="IndiGo")]

    ranked = rank_fares(candidates, _request(sample_travel_date, preferred_airline="IndiGo"))

    assert ranked[0].flight_number == "6E200"


def test_preferred_departure_ranks_matching_time(sample_travel_date):
    candidates = [
        _fare("6E200", 7000, departure="08:10"),
        _fare("AI100", 7000, departure="19:10"),
    ]

    ranked = rank_fares(candidates, _request(sample_travel_date, preferred_departure="evening"))

    assert ranked[0].flight_number == "AI100"


def test_equal_fares_have_deterministic_order(sample_travel_date):
    candidates = [_fare("6E200", 7000), _fare("AI100", 7000)]

    ranked = rank_fares(candidates, _request(sample_travel_date))

    assert [fare.flight_number for fare in ranked] == ["6E200", "AI100"]


def test_missing_preferences_are_safe_and_fare_remains_ranked(sample_travel_date):
    candidates = [
        _fare("6E200", 7000, departure="not a time"),
        _fare("AI100", 6000, airline=None, departure=None, status="unverified"),
    ]

    ranked = rank_fares(
        candidates,
        _request(sample_travel_date, preferred_airline="IndiGo", preferred_departure="morning"),
    )

    assert ranked[0].flight_number == "AI100"


def test_missing_prices_do_not_crash_and_keep_deterministic_order(sample_travel_date):
    candidates = [
        _fare("6E200", None, departure="08:10"),
        _fare("AI100", None, departure="19:10"),
    ]

    ranked = rank_fares(
        candidates,
        _request(sample_travel_date, preferred_departure="evening"),
    )

    assert [fare.flight_number for fare in ranked] == ["AI100", "6E200"]


def test_departure_time_parsing_handles_uppercase_and_am_pm(sample_travel_date):
    candidates = [
        _fare("6E200", 7000, departure="08:10 AM"),
        _fare("AI100", 7000, departure="07:45 PM"),
    ]

    ranked = rank_fares(
        candidates,
        _request(sample_travel_date, preferred_departure="evening"),
    )

    assert ranked[0].flight_number == "AI100"
