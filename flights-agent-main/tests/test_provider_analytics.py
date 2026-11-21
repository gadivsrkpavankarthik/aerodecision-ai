"""Phase 6 tests: provider reliability analytics."""
import tempfile
from pathlib import Path

from analytics import SearchAnalyticsStore
from models import FlightSearchResult, SearchRequest, FareResult


def _result(**kw):
    fare = FareResult(
        flight_number="AI101", airline="Air India", origin="DEL", destination="BOM",
        travel_date="2026-12-20", price=5000, currency="INR", price_available=True,
        price_status="verified", stops=0, constraint_status="eligible",
    )
    req = SearchRequest(origin_code="DEL", destination_code="BOM", travel_date="2026-12-20")
    data = dict(
        query="DEL to BOM", search_request=req, fares_found=[fare],
        valid_candidates=[fare], best_flight=fare, matched_fares=[fare],
        formatted_response="ok", provider_statuses={"aviationstack": "success", "ignav": "success"},
        provider_durations_ms={"aviationstack": 100, "ignav": 200},
        flights_found=[], recommendations=[fare],
    )
    data.update(kw)
    return FlightSearchResult(**data)


def test_provider_events_and_reliability_aggregate():
    with tempfile.TemporaryDirectory() as td:
        store = SearchAnalyticsStore(Path(td) / "a.sqlite3")
        store.record_search(_result(), 50)
        store.record_search(_result(
            provider_statuses={"aviationstack": "error", "ignav": "success"},
            provider_durations_ms={"aviationstack": 30, "ignav": 90},
            fares_found=[], valid_candidates=[], best_flight=None,
            matched_fares=[], flights_found=[], recommendations=[],
        ), 40)
        rel = {p["provider"]: p for p in store.provider_reliability()}
        assert rel["aviationstack"]["total_calls"] == 2
        assert rel["aviationstack"]["success_calls"] == 1
        assert rel["aviationstack"]["failure_calls"] == 1
        assert rel["aviationstack"]["success_pct"] == 50.0
        assert rel["ignav"]["avg_duration_ms"] == 145.0
        assert rel["ignav"]["avg_records_per_call"] == 0.5


def test_search_record_has_rejection_counts():
    with tempfile.TemporaryDirectory() as td:
        store = SearchAnalyticsStore(Path(td) / "a.sqlite3")
        rejected = FareResult(
            flight_number="AI102", airline="Air India", origin="DEL", destination="BOM",
            travel_date="2026-12-20", price=99999, currency="INR", price_available=True,
            price_status="verified", stops=0, constraint_status="over_budget",
            constraint_reason="over budget",
        )
        fare = FareResult(
            flight_number="AI101", airline="Air India", origin="DEL", destination="BOM",
            travel_date="2026-12-20", price=5000, currency="INR", price_available=True,
            price_status="verified", stops=0, constraint_status="eligible",
        )
        store.record_search(_result(matched_fares=[fare, rejected]), 50)
        record = store.get_searches(limit=1)[0]
        assert record["eligible_count"] == 1
        assert record["rejected_count"] == 1
        assert record["recommendation_count"] == 1
