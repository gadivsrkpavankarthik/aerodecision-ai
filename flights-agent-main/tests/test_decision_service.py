from models import FareResult, FlightSearchResult, SearchRequest
from services.decision_service import build_decision_workspace
from config import config

def test_search_pipeline_composes_all_decision_engines(sample_travel_date):
    fare=FareResult(flight_number='AI101',origin='DEL',destination='BOM',travel_date=sample_travel_date,departure_time='09:00',arrival_time='11:00',price=2500,currency='INR',price_available=True,price_status='verified',schedule_matched=True,retrieved_at='2026-11-13T00:00:00+00:00')
    request=SearchRequest(origin_code='DEL',destination_code='BOM',travel_date=sample_travel_date,budget=5000)
    result=FlightSearchResult(query='Delhi to Mumbai',search_request=request,fares_found=[fare],valid_candidates=[fare],best_flight=fare,formatted_response='ok',provider_statuses={'aviationstack':'ok','ignav':'ok'})
    workspace=build_decision_workspace(result)
    assert workspace['evidence']['AI101']['evidence_score'] > 0
    assert workspace['cost']['flight_cost']==2500
    assert workspace['cost']['complete_trip_cost'] is None
    assert 'cheapest' in workspace['recommendations']['categories']
    assert workspace['disruption_scenarios']['60']['delay_minutes']==60
    assert workspace['source_status']['ignav']=='ok'

def test_no_budget_uses_documented_default_comparison_currency(sample_travel_date,monkeypatch):
    import engines.recommendation_engine as recommendation_engine
    from services.fx_service import FXUnavailable
    monkeypatch.setattr(recommendation_engine,'convert',lambda *a,**k: (_ for _ in ()).throw(FXUnavailable('ECB timeout')))
    monkeypatch.setattr(config,'default_comparison_currency','EUR')
    fare=FareResult(flight_number='AI101',origin='DEL',destination='BOM',travel_date=sample_travel_date,price=2500,currency='INR',price_available=True,price_status='verified')
    request=SearchRequest(origin_code='DEL',destination_code='BOM',travel_date=sample_travel_date,currency='USD',budget=None)
    result=FlightSearchResult(query='x',search_request=request,fares_found=[fare],valid_candidates=[fare],best_flight=fare,formatted_response='ok')
    workspace=build_decision_workspace(result)
    assert workspace['comparison_currency']=='EUR'
    assert workspace['recommendations']['comparison_currency']=='EUR'
