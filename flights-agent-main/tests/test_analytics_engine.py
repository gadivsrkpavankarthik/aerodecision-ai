"""Analytics engine tests — real calculations, honest empty states."""
import pytest
from engines.analytics_engine import (
    fare_intelligence, journey_efficiency, provider_performance,
    matching_performance, verification_quality, decision_engine,
    interaction_analytics, risk_insights, build_insights,
)


def _fare(**kw):
    f = {"flight_number": "6E1", "price": 5000.0, "currency": "INR",
         "comparison_amount": 5000.0, "comparison_currency": "INR",
         "constraint_status": "eligible", "schedule_matched": False,
         "itinerary_kind": "direct", "price_status": "verified",
         "stops": 0, "verification": {"fare": "VERIFIED", "schedule": "UNKNOWN",
                                      "seats": "UNKNOWN", "booking": "UNKNOWN"},
         "freshness": "FRESH", "risk_level": "NOT_APPLICABLE", "score": 80.0}
    f.update(kw)
    return f


def _search(**kw):
    s = {"search_id": "s1", "searched_at": "2026-11-21T00:00:00Z",
         "search_outcome": "result_found", "error_code": None,
         "schedule_count": 0, "fare_count": 2, "eligible_count": 2,
         "rejected_count": 0, "matched_schedule_count": 0,
         "verified_fare_count": 1, "unverified_fare_count": 1,
         "flight_found": True, "budget": 20000, "budget_currency": "INR",
         "fares": [_fare(), _fare(flight_number="6E2", price=7000.0,
                                  comparison_amount=7000.0)]}
    s.update(kw)
    return s


class TestFareIntelligence:
    def test_basic_stats(self):
        r = fare_intelligence([_search()])
        assert r["status"] == "ok"
        m = r["metrics"]
        assert m["cheapest"]["value"] == 5000.0
        assert m["highest"]["value"] == 7000.0
        assert m["average"]["value"] == 6000.0
        assert m["median"]["value"] == 6000.0
        assert m["eligible_count"]["value"] == 2

    def test_empty_is_honest(self):
        r = fare_intelligence([])
        assert r["status"] == "no_data"
        assert r["sample_size"] == 0

    def test_budget_compliance(self):
        r = fare_intelligence([_search(budget=6000)])
        assert r["metrics"]["within_budget"]["value"] == 1
        assert r["metrics"]["outside_budget"]["value"] == 1

    def test_no_fx_mixing(self):
        s = _search()
        s["fares"][1]["comparison_amount"] = None  # no FX → excluded from currency stats
        r = fare_intelligence([s])
        assert r["metrics"]["cheapest"]["sample_size"] == 1
        assert r["metrics"]["fx_converted_count"]["value"] == 1


class TestJourney:
    def test_naive_times_excluded(self):
        s = _search()
        s["fares"][0]["departure_at"] = "10:00"  # naive, no tz
        s["fares"][0]["arrival_at"] = "12:00"
        s["fares"][1]["departure_at"] = "2026-12-20T10:00:00+05:30"
        s["fares"][1]["arrival_at"] = "2026-12-20T12:30:00+05:30"
        r = journey_efficiency([s])
        assert r["status"] == "ok"
        assert r["metrics"]["average_minutes"]["value"] == 150
        assert r["excluded"]["naive_or_invalid"] == 1

    def test_no_tz_data_is_honest(self):
        r = journey_efficiency([_search()])
        assert r["status"] == "no_data"


class TestProviderPerformance:
    def test_three_events_shows_three(self):
        events = [
            {"provider": "ignav", "status": "success", "duration_ms": 100},
            {"provider": "ignav", "status": "success", "duration_ms": 200},
            {"provider": "aviationstack", "status": "future_date_unavailable", "duration_ms": 300},
        ]
        r = provider_performance(events)
        assert r["providers"]["ignav"]["total_requests"] == 2
        assert r["providers"]["ignav"]["success_rate_pct"] == 100.0
        assert r["providers"]["aviationstack"]["success_rate_pct"] == 0.0
        assert r["providers"]["aviationstack"]["future_date_unavailable"] == 1

    def test_empty_telemetry(self):
        r = provider_performance([])
        assert r["status"] == "no_data"


class TestMatchingVerificationDecision:
    def test_matching_funnel(self):
        r = matching_performance([_search(), _search(search_id="s2", schedule_count=5, fare_count=0)])
        assert r["funnel"]["searches"] == 2
        assert r["funnel"]["fare_records"] == 2
        assert r["funnel"]["searches_with_schedules_only"] == 1

    def test_verification_facets_independent(self):
        r = verification_quality([_search()])
        assert r["metrics"]["facet_distribution"]["fare"]["VERIFIED"] == 2
        assert r["metrics"]["facet_distribution"]["schedule"]["UNKNOWN"] == 2

    def test_decision_outcomes(self):
        r = decision_engine([_search(), _search(search_outcome="no_result", flight_found=False)])
        assert r["metrics"]["search_outcomes"]["result_found"] == 1
        assert r["metrics"]["search_outcomes"]["no_result"] == 1

    def test_risk_distribution(self):
        r = risk_insights([_search()])
        assert r["metrics"]["risk_distribution"]["value"]["NOT_APPLICABLE"] == 2

    def test_interactions_not_bookings(self):
        r = interaction_analytics([{"event_type": "booking_link_clicked"}])
        assert r["metrics"]["event_counts"]["booking_link_clicked"] == 1

    def test_build_insights_shape(self):
        d = build_insights([_search()], [], [])
        for key in ["fares", "journey", "recommendations", "risks", "providers",
                    "matching", "verification", "decision_engine", "interactions"]:
            assert key in d
