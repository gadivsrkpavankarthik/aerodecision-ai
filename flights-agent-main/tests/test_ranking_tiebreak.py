"""Phase 3 tests: deterministic tie-breaking and weight validation."""
from datetime import date, timedelta

import pytest

from models import FareResult, SearchRequest
from ranking import rank_fares, compute_category_winners


def _fare(number, price, **kw):
    d = (date.today() + timedelta(days=30)).isoformat()
    data = dict(
        flight_number=number, airline="IndiGo", origin="DEL", destination="BOM",
        travel_date=d, price=price, currency="INR", price_available=True,
        price_status="verified", stops=0,
        departure_time="08:00", departure_at="2026-12-20T08:00:00+05:30",
        arrival_at="2026-12-20T10:00:00+05:30",
    )
    data.update(kw)
    return FareResult(**data)


def _req():
    d = (date.today() + timedelta(days=30)).isoformat()
    return SearchRequest(origin_code="DEL", destination_code="BOM", travel_date=d)


def test_equal_scores_break_ties_deterministically():
    # Identical fares except flight number -> stable ordering by flight_number.
    a = _fare("AI200", 5000)
    b = _fare("AI100", 5000)
    ranked = rank_fares([a, b], _req())
    assert [f.flight_number for f in ranked] == ["AI100", "AI200"]
    ranked2 = rank_fares([b, a], _req())
    assert [f.flight_number for f in ranked2] == ["AI100", "AI200"]


def test_lower_price_wins_tie_after_score():
    cheap = _fare("AI100", 4000)
    dear = _fare("AI200", 9000)
    ranked = rank_fares([dear, cheap], _req())
    assert ranked[0].flight_number == "AI100"


def test_missing_price_not_treated_as_positive():
    priced = _fare("AI100", 5000)
    unpriced = _fare("AI200", None, price_available=False, price_status="unavailable")
    ranked = rank_fares([unpriced, priced], _req())
    assert ranked[0].flight_number == "AI100"
    assert "price" in (unpriced.score_breakdown or {}).get("missing_factors", [])


def test_weights_validated_non_negative(monkeypatch):
    from config import config
    monkeypatch.setattr(config, "ranking_price_weight", -1.0)
    # Negative weight is excluded from scoring rather than crashing.
    ranked = rank_fares([_fare("AI100", 5000), _fare("AI200", 6000)], _req())
    assert len(ranked) == 2


def test_category_winners_with_missing_duration():
    no_time = _fare("AI100", 5000, departure_at=None, arrival_at=None)
    winners = compute_category_winners([no_time], _req())
    assert winners["fastest"]["label"] == "INSUFFICIENT DATA"
    assert winners["cheapest"]["flight_number"] == "AI100"
