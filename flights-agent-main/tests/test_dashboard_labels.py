"""Regression tests for dashboard label honesty and connection-risk states."""
import re
import pytest

from models import FareResult
from engines.transfer_risk_engine import analyze_transfer


def _fare(**kw):
    base = dict(
        flight_number="6E999", airline="Test Air", airline_code="6E",
        origin="DEL", destination="BOM", travel_date="2026-12-20",
        departure_time="10:00", arrival_time="12:00",
        price=5000.0, currency="INR", price_status="verified",
        booking_url="https://example.com/b", source="ignav",
    )
    base.update(kw)
    return FareResult(**base)


class TestDashboardLabels:
    def test_template_uses_top_fare_option_not_verified_fare(self):
        html = open("web_app.py", encoding="utf-8").read()
        assert "TOP VERIFIED FARE" not in html
        assert "TOP FARE OPTION" in html
        assert "TOP VERIFIED FLIGHT" in html

    def test_template_uses_known_fare_not_trip_cost(self):
        html = open("web_app.py", encoding="utf-8").read()
        assert "Trip cost:" not in html
        assert "Known fare:" in html

    def test_label_condition_requires_schedule_match(self):
        html = open("web_app.py", encoding="utf-8").read()
        assert "best.schedule_matched ? 'TOP VERIFIED FLIGHT'" in html


class TestConnectionRiskStates:
    def test_fare_only_unconfirmed_direct_is_unknown(self):
        fare = _fare(itinerary_kind="direct", schedule_matched=False)
        r = analyze_transfer(fare)
        assert r["risk_level"] == "UNKNOWN"

    def test_confirmed_direct_is_not_applicable(self):
        fare = _fare(itinerary_kind="direct", schedule_matched=True)
        r = analyze_transfer(fare)
        assert r["risk_level"] == "NOT_APPLICABLE"

    def test_self_transfer_gets_real_assessment(self):
        from models import ItineraryLeg
        fare = _fare(
            itinerary_kind="connecting_self_transfer",
            schedule_matched=False,
            legs=[
                ItineraryLeg(leg_index=1, flight_number="6E1", origin="DEL", destination="BLR", travel_date="2026-12-20",
                             departure_at="2026-12-20T10:00:00+05:30", arrival_at="2026-12-20T12:00:00+05:30"),
                ItineraryLeg(leg_index=2, flight_number="6E2", origin="BLR", destination="BOM", travel_date="2026-12-20",
                             departure_at="2026-12-20T14:00:00+05:30", arrival_at="2026-12-20T16:00:00+05:30"),
            ],
            layover_minutes=120,
        )
        r = analyze_transfer(fare)
        assert r["risk_level"] in {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}
        assert r.get("ticket_structure") == "separate_tickets"
