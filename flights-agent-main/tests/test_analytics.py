from analytics import SearchAnalyticsStore
from models import FareResult, FlightSearchResult, SearchRequest
import sqlite3


def test_search_analytics_records_currency_aware_kpis(tmp_path, sample_travel_date):
    request = SearchRequest(
        origin_code="DEL",
        destination_code="BOM",
        travel_date=sample_travel_date,
        budget=200.0,
        currency="USD",
    )
    fare = FareResult(
        flight_number="AI865",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=150.0,
        currency="USD",
        price_available=True,
        price_status="verified",
        schedule_matched=True,
    )
    rupee_fare = FareResult(
        flight_number="6E101",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=12000.0,
        currency="INR",
        price_available=True,
        price_status="verified",
    )
    result = FlightSearchResult(
        query="test",
        search_request=request,
        fares_found=[fare, rupee_fare],
        matched_fares=[fare],
        valid_candidates=[fare],
        best_flight=fare,
        formatted_response="selected",
        request_id="request-test",
        provider_statuses={"aviationstack": "success", "ignav": "success"},
        provider_durations_ms={"aviationstack": 100, "ignav": 200},
    )
    store = SearchAnalyticsStore(tmp_path / "analytics.sqlite3")

    store.record_search(result, duration_ms=1250)
    record = store.get_searches()[0]

    assert record["budget_currency"] == "USD"
    assert record["matched_schedule_count"] == 1
    assert record["verified_fare_count"] == 2
    assert record["search_outcome"] == "result_found"
    assert record["budget_met"] is True
    assert record["budget_utilization_pct"] == 75.0
    assert record["search_id"]
    assert record["request_id"] == "request-test"
    assert record["lowest_fare_in_budget_currency"] == 150
    assert record["aviationstack_status"] == "success"
    assert record["ignav_duration_ms"] == 200
    assert record["availability_status"] == "unknown"
    assert record["search_duration_ms"] == 1250


def test_legacy_analytics_schema_migration_preserves_rows_and_creates_backup(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE searches (id INTEGER PRIMARY KEY, searched_at TEXT NOT NULL)')
        db.execute("INSERT INTO searches(id,searched_at) VALUES (1,'2025-01-01T00:00:00+00:00')")
    store=SearchAnalyticsStore(path)
    with sqlite3.connect(path) as db:
        row=db.execute('SELECT id,searched_at,schema_version,record_json FROM searches').fetchone()
    assert row==(1,'2025-01-01T00:00:00+00:00',1,'{}')
    assert len(store.get_searches())==1
    assert store.get_searches()[0]['search_outcome']=='no_result'
    assert path.with_suffix('.sqlite3.bak').exists()
