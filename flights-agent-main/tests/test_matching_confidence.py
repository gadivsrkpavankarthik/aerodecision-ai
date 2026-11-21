"""Phase 2 tests: match confidence, freshness expiry, booking states."""
from datetime import datetime, timedelta, timezone

from models import FareResult, FlightOption
from matching import match_flights_with_fares, classify_match
from engines.verification_engine import apply_verification


def _fare(**kw):
    data = dict(
        flight_number="6E327", airline="IndiGo", origin="DEL", destination="BOM",
        travel_date="2026-12-20", departure_time="08:10", arrival_time="10:20",
        price=5000, currency="INR", price_available=True, price_status="verified",
        stops=0, retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
    data.update(kw)
    return FareResult(**data)


def _flight(**kw):
    data = dict(
        flight_number="6E327", airline="IndiGo", origin="DEL", destination="BOM",
        travel_date="2026-12-20", departure_time="08:10", arrival_time="10:20",
        stops=0, retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
    data.update(kw)
    return FlightOption(**data)


def test_exact_match_sets_confidence_and_fields():
    out = match_flights_with_fares([_flight()], [_fare()])
    assert len(out) == 1
    assert out[0].schedule_matched is True
    assert out[0].match_confidence == "EXACT_MATCH"
    assert "flight_number" in out[0].match_fields
    assert "origin" in out[0].match_fields


def test_different_airline_no_match():
    fare = _fare(airline="Air India", flight_number="AI101")
    out = match_flights_with_fares([_flight()], [fare])
    assert all(not r.schedule_matched for r in out)
    fare_only = [r for r in out if r.flight_number == "AI101"]
    assert fare_only and fare_only[0].match_confidence == "NO_MATCH"


def test_ambiguous_match_not_merged():
    # One fare exact-matching two different schedule records -> no verified association.
    fare = _fare()
    f1 = _flight(departure_time="08:10")
    f2 = _flight(departure_time="08:12")  # within ±15min of the same fare
    out = match_flights_with_fares([f1, f2], [fare])
    assert not any(r.schedule_matched for r in out)
    assert any(r.match_confidence in {"PARTIAL_MATCH", "NO_MATCH"} for r in out)


def test_duplicate_schedules_deduplicated():
    out = match_flights_with_fares([_flight(), _flight()], [_fare()])
    assert len(out) == 1


def test_expired_fare_evidence_downgrades_to_unverified():
    stale = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    fare = apply_verification(_fare(retrieved_at=stale))
    assert fare.verification.fare == "UNVERIFIED"
    assert fare.verification.freshness_status == "STALE"
    assert any("expired" in n for n in fare.verification.notes)


def test_missing_timestamp_is_not_fresh():
    fare = apply_verification(_fare(retrieved_at=None))
    assert fare.verification.freshness_status == "UNKNOWN"


def test_booking_states():
    assert apply_verification(_fare()).booking_state == "NO_LINK"
    linked = apply_verification(_fare(booking_url="https://example.com/book", web_verification_status="passed"))
    assert linked.booking_state == "LINK_AVAILABLE"
    assert linked.verification.booking == "VERIFIED"
    failed = apply_verification(_fare(booking_url="https://example.com/book", web_verification_status="failed"))
    assert failed.booking_state == "LINK_UNREACHABLE"
    assert failed.verification.booking == "NOT CONFIRMED"
    partial = apply_verification(_fare(booking_url="https://example.com/book", web_verification_status="partial"))
    assert partial.booking_state == "BOOKING_DETAILS_UNCONFIRMED"


def test_transfer_risk_reasons_structured():
    from models import ItineraryLeg
    from engines.transfer_risk_engine import analyze_transfer
    first = ItineraryLeg(leg_index=1, flight_number='AI1', origin='DEL', destination='BOM',
                         travel_date='2026-12-20', departure_at='2026-12-20T20:00:00+05:30',
                         arrival_at='2026-12-20T22:00:00+05:30')
    second = ItineraryLeg(leg_index=2, flight_number='AI2', origin='BOM', destination='MAA',
                          travel_date='2026-12-21', departure_at='2026-12-21T02:00:00+05:30',
                          arrival_at='2026-12-21T04:00:00+05:30')
    result = analyze_transfer(_fare(itinerary_kind='connecting_self_transfer', legs=[first, second], layover_minutes=240))
    codes = {r['code'] for r in result['risk_reasons']}
    assert 'separate_tickets' in codes and 'no_missed_connection_protection' in codes
    assert 'overnight_connection' in codes
    assert result['risk_level'] in {'LOW', 'MEDIUM', 'HIGH', 'UNKNOWN'}
