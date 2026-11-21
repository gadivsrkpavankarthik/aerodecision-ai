"""Unit tests for Web App endpoints."""

import pytest
from fastapi.testclient import TestClient
import web_app
from analytics import SearchAnalyticsStore
from models import FareResult, FlightSearchResult, SearchRequest
from services.trip_service import TripStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        web_app,
        "analytics_store",
        SearchAnalyticsStore(tmp_path / "analytics.sqlite3"),
    )
    monkeypatch.setattr(web_app, "trip_store", TripStore(tmp_path / "analytics.sqlite3"))
    return TestClient(web_app.app)


def test_decision_analysis_and_trip_save_endpoints(client):
    fare={"flight_number":"AI101","origin":"DEL","destination":"BOM","travel_date":"2026-12-20","price":2500,"currency":"INR","price_available":True,"price_status":"verified","schedule_matched":True,"retrieved_at":"2026-11-13T00:00:00+00:00"}
    evidence=client.post('/api/evidence/analyze',json={"fare":fare})
    assert evidence.status_code==200
    assert evidence.json()['evidence_score'] > 0
    saved=client.post('/api/trips/save',json={"title":"Delhi to Mumbai","payload":{"selected_itinerary":fare}})
    assert saved.status_code==200
    trip_id=saved.json()['trip_id']
    assert client.get(f'/api/trips/{trip_id}').json()['payload']['selected_itinerary']['flight_number']=='AI101'
    assert client.get(f'/api/trips/{trip_id}/report?format=csv').headers['content-type'].startswith('text/csv')


def test_search_response_includes_composed_decision_workspace(client, monkeypatch, sample_travel_date):
    fare=FareResult(flight_number='AI101',origin='DEL',destination='BOM',travel_date=sample_travel_date,departure_time='09:00',arrival_time='11:00',price=2500,currency='INR',price_available=True,price_status='verified',schedule_matched=True,retrieved_at='2026-11-13T00:00:00+00:00')
    result=FlightSearchResult(query='Delhi to Mumbai',search_request=SearchRequest(origin_code='DEL',destination_code='BOM',travel_date=sample_travel_date),fares_found=[fare],valid_candidates=[fare],best_flight=fare,formatted_response='ok')
    monkeypatch.setattr(web_app.FlightOptionFinderAgent,'run',lambda *args,**kwargs:result)
    response=client.post('/api/search',json={'origin':'Delhi','destination':'Mumbai','travel_date':sample_travel_date})
    assert response.status_code==200
    workspace=response.json()['decision_workspace']
    assert workspace['evidence']['AI101']['evidence_score'] > 0
    assert workspace['cost']['flight_cost']==2500
    assert 'cheapest' in workspace['recommendations']['categories']
    assert workspace['recommendations']['fx_disclaimer']=='Converted amounts use ECB reference exchange rates for comparison. Actual booking charges may differ due to provider pricing, card conversion rates, and fees.'
    assert response.json()['best_flight']['trust_status']==response.json()['valid_candidates'][0]['trust_status']
    assert workspace['recommendations']['categories']['cheapest']['flight']['price']==response.json()['best_flight']['price']


def test_web_app_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "T10 Flight Option Finder" in response.text
    assert 'id="formCurrency"' in response.text
    assert 'value="USD"' in response.text
    assert "SELF-TRANSFER ESTIMATE" in response.text
    assert "No single booking link — book each leg separately." in response.text
    assert "Each flight leg must be booked separately" in response.text
    assert "SCHEDULE MATCH" in response.text
    assert "SEAT AVAILABILITY" in response.text
    assert "formMaxStops" in response.text
    assert "decisionWorkspaceCard" in response.text
    assert "runDisruptionScenario" in response.text


def test_web_app_api_search(client, sample_travel_date):
    response = client.post("/api/search", json={"query": f"Delhi to Mumbai on {sample_travel_date} under ₹8,000"})
    assert response.status_code == 200
    data = response.json()
    assert "query" in data
    assert "trace" in data
    assert "formatted_response" in data
    assert "decision_workspace" in data
    assert "source_status" in data["decision_workspace"]
    assert len(data["trace"]) > 0

    analytics = client.get("/api/analytics/searches")
    assert analytics.status_code == 200
    assert len(analytics.json()) == 1
    assert analytics.json()[0]["travel_date"] == sample_travel_date
    assert analytics.json()[0]["search_duration_ms"] >= 0
    assert "search_outcome" in analytics.json()[0]
    assert "verified_fare_count" in analytics.json()[0]


def test_structured_search_passes_selected_currency(client, monkeypatch, sample_travel_date):
    captured = {}

    class StubAgent:
        def __init__(self, memory):
            pass

        def run(self, query, request_id=None, overrides=None):
            captured.update(overrides or {})
            request = SearchRequest(
                origin_city="Delhi",
                destination_city="New York",
                origin_code="DEL",
                destination_code="JFK",
                travel_date=sample_travel_date,
                budget=overrides["budget"],
                currency=overrides["currency"],
                passengers=overrides["passengers"],
                cabin_class=overrides["cabin_class"],
                preferred_departure=overrides["preferred_departure"],
                max_stops=overrides["max_stops"],
            )
            return FlightSearchResult(
                query=query,
                search_request=request,
                formatted_response="No result",
                request_id=request_id,
            )

    monkeypatch.setattr(web_app, "FlightOptionFinderAgent", StubAgent)
    response = client.post(
        "/api/search",
        json={
            "origin": "Delhi",
            "destination": "New York",
            "travel_date": sample_travel_date,
            "budget": 500,
            "currency": "USD",
            "max_stops": 0,
        },
    )

    assert response.status_code == 200
    assert captured["currency"] == "USD"
    assert captured["max_stops"] == 0
    assert response.json()["search_request"]["currency"] == "USD"


def test_structured_search_rejects_invalid_max_stops(client, sample_travel_date):
    response = client.post(
        "/api/search",
        json={
            "origin": "Delhi",
            "destination": "Mumbai",
            "travel_date": sample_travel_date,
            "max_stops": 4,
        },
    )

    assert response.status_code == 422
