"""Phase 5 tests: result-quality evaluator."""
from datetime import datetime, timedelta, timezone

from models import FareResult, IndependentVerification
from engines.result_quality_evaluator import evaluate_result_quality


def _fare(**kw):
    data = dict(
        flight_number="AI101", airline="Air India", origin="DEL", destination="BOM",
        travel_date="2026-12-20", price=5000, currency="INR", price_available=True,
        price_status="verified", stops=0, schedule_matched=True,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        schedule_retrieved_at=datetime.now(timezone.utc).isoformat(),
        availability_status="provider_confirmed",
        match_confidence="EXACT_MATCH",
    )
    data.update(kw)
    return FareResult(**data)


def _verified_fare(**kw):
    fare = _fare(**kw)
    fare.verification = IndependentVerification(
        fare="VERIFIED", schedule="VERIFIED", seats="VERIFIED", booking="VERIFIED",
        freshness_status="FRESH",
    )
    fare.booking_state = "LINK_AVAILABLE"
    return fare


def test_ready_when_all_facets_verified_fresh():
    result = evaluate_result_quality(_verified_fare(), provider_statuses={"aviationstack": "success", "ignav": "success"})
    assert result["readiness_status"] == "READY"
    assert result["matching_confidence"] == "EXACT_MATCH"
    assert "does not guarantee" in result["caveat"]


def test_insufficient_evidence_when_fare_unconfirmed():
    fare = _fare(price=None, price_available=False, price_status="unavailable")
    result = evaluate_result_quality(fare)
    assert result["readiness_status"] == "INSUFFICIENT_EVIDENCE"
    assert any(m.startswith("critical_fields") and "price" in m for m in result["missing_evidence"])


def test_partially_verified_when_seats_unknown():
    fare = _verified_fare()
    fare.verification.seats = "UNKNOWN"
    result = evaluate_result_quality(fare, provider_statuses={"aviationstack": "success", "ignav": "success"})
    assert result["readiness_status"] == "PARTIALLY_VERIFIED"
    assert "seat_availability" in result["missing_evidence"]


def test_expired_evidence_detected():
    fare = _verified_fare()
    fare.verification.freshness_status = "STALE"
    result = evaluate_result_quality(fare)
    assert result["expired_evidence"]
    assert result["readiness_status"] != "READY"


def test_currency_incomparable_flagged():
    fare = _fare(currency="USD", fx_status="unavailable")
    result = evaluate_result_quality(fare, comparison_currency="INR")
    assert "currency_comparability" in result["missing_evidence"]


def test_high_connection_risk_flagged():
    from models import ItineraryLeg
    first = ItineraryLeg(leg_index=1, flight_number='AI1', origin='DEL', destination='BOM',
                         travel_date='2026-12-20', departure_at='2026-12-20T20:00:00+05:30',
                         arrival_at='2026-12-20T22:00:00+05:30')
    second = ItineraryLeg(leg_index=2, flight_number='AI2', origin='BOM', destination='MAA',
                          travel_date='2026-12-21', departure_at='2026-12-21T02:00:00+05:30',
                          arrival_at='2026-12-21T04:00:00+05:30')
    fare = _fare(itinerary_kind='connecting_self_transfer', legs=[first, second], layover_minutes=30)
    result = evaluate_result_quality(fare)
    assert result["connection_risk"] == "HIGH"
