from engines.evidence_engine import analyze_evidence
from engines.trip_cost_engine import estimate_trip_cost
from engines.disruption_engine import simulate_disruption
from engines.alternative_planner import generate_alternatives
from engines.readiness_engine import check_readiness
from engines.recommendation_engine import generate_recommendations
from services.fx_service import FXRate, FXUnavailable
import engines.recommendation_engine as recommendation_engine
from decimal import Decimal
from models import FareResult
from models import ItineraryLeg
from engines.transfer_risk_engine import analyze_transfer
from engines.recommendation_engine import generate_recommendations

def fare(number='AI101', price=1000, **kwargs):
    data=dict(flight_number=number,origin='DEL',destination='BOM',travel_date='2026-12-20',price=price,currency='INR',price_available=True,price_status='verified',schedule_matched=True,retrieved_at='2026-11-13T00:00:00+00:00')
    data.update(kwargs); return FareResult(**data)

def test_evidence_score_is_explainable_and_not_probability():
    result=analyze_evidence(fare())
    assert 0 <= result['evidence_score'] <= 100
    assert 'probability' in result['evidence_reasons'][-1]

def test_trip_cost_retains_unknown_expenses():
    result=estimate_trip_cost([fare()], {'baggage':None,'airport_transfer':500}, passengers=2)
    assert result['flight_cost']==2000 and result['known_cost']==2500
    assert result['estimated_total_cost'] is None
    assert 'baggage' in result['unknown_expenses']

def test_disruption_is_scenario_only():
    result=simulate_disruption(fare(),delay_minutes=60)
    assert result['scenario']=='60-minute delay'
    assert any('does not predict' in item for item in result['limitations'])

def test_alternatives_are_distinct_and_limited():
    result=generate_alternatives([fare('AI101'),fare('AI102'),fare('AI103'),fare('AI104')])
    assert len(result)==3 and len({x['itinerary']['flight_number'] for x in result})==3

def test_recommendations_apply_hard_transfer_constraint():
    direct=fare('AI101',1000)
    transfer=fare('AI202',500,itinerary_kind='connecting_self_transfer')
    result=generate_recommendations([direct,transfer],prohibit_self_transfer=True)
    assert result['categories']['cheapest']['flight']['flight_number']=='AI101'
    assert result['excluded_count']==1

def test_readiness_marks_unsourced_requirements_unknown():
    result=check_readiness(fare())
    assert result['ready'] is False
    assert result['requirements']['travel_documents_reviewed'] is False

def test_overnight_separate_ticket_risk_uses_available_layover():
    first=ItineraryLeg(leg_index=1,flight_number='AI101',origin='DEL',destination='BOM',travel_date='2026-12-20',departure_at='2026-12-20T20:00:00+05:30',arrival_at='2026-12-20T22:00:00+05:30')
    second=ItineraryLeg(leg_index=2,flight_number='AI202',origin='BOM',destination='MAA',travel_date='2026-12-21',departure_at='2026-12-21T02:00:00+05:30',arrival_at='2026-12-21T04:00:00+05:30')
    result=analyze_transfer(fare(itinerary_kind='connecting_self_transfer',legs=[first,second],layover_minutes=240))
    assert result['category']=='moderate_concern'
    assert result['risk_score'] < 100

def test_direct_flight_is_not_reported_as_zero_risk():
    result=analyze_transfer(fare())
    assert result['risk_score'] is None
    assert result['category']=='not_applicable'
    assert result['risk_level']=='NOT_APPLICABLE'

def test_cheapest_returns_all_equal_fares_and_keeps_currencies_separate(monkeypatch):
    def fx(amount,source,target):
        if source!=target: raise FXUnavailable('ECB has no reference rate for USD; unsupported currency.')
        return Decimal(str(amount)),FXRate(Decimal(1),source,target,'identity','2026-11-14','now','not_required')
    monkeypatch.setattr(recommendation_engine,'convert',fx)
    one=fare('AI101',1000,currency='INR')
    two=fare('AI102',1000,currency='INR')
    foreign=fare('EK201',10,currency='USD')
    result=generate_recommendations([one,two,foreign])
    cheap=result['categories']['cheapest']
    assert cheap['tie'] is True
    assert len(cheap['candidates'])==2
    assert cheap['currency']=='INR'
    assert 'EK201' in result['incomparable_fares']

def test_fastest_requires_valid_timestamps_and_qualifies_unmatched():
    missing=fare('AI101',1000)
    result=generate_recommendations([missing])
    assert result['categories']['fastest']['label']=='INSUFFICIENT DATA TO RANK'
    valid=fare('AI202',1200,schedule_matched=False,departure_at='2026-12-20T23:00:00+05:30',arrival_at='2026-12-21T01:00:00+05:30')
    result=generate_recommendations([valid])
    assert result['categories']['fastest']['label']=='FASTEST PROVIDER-REPORTED OPTION'

def test_lowest_risk_does_not_return_unassessed_winner():
    result=generate_recommendations([fare()])
    assert result['categories']['lowest_assessed_connection_risk']['flight'] is None
    assert result['categories']['lowest_assessed_connection_risk']['label']=='NOT APPLICABLE'
    transfer_result=generate_recommendations([fare('AI202',itinerary_kind='connecting_self_transfer')])
    assert transfer_result['categories']['lowest_assessed_connection_risk']['label']=='INSUFFICIENT DATA TO RANK'

def test_recommendations_normalize_currency_without_mutating_original_fare(monkeypatch):
    def fx(amount, source, target):
        if source=='USD': return Decimal('900.00'),FXRate(Decimal('90'),source,target,'ECB test','2026-11-13','2026-11-14T00:00:00+00:00','fresh')
        return Decimal(str(amount)),FXRate(Decimal('1'),source,target,'identity','2026-11-14','2026-11-14T00:00:00+00:00','not_required')
    monkeypatch.setattr(recommendation_engine,'convert',fx)
    rupee=fare('AI101',1000,currency='INR')
    dollar=fare('EK101',10,currency='USD')
    result=generate_recommendations([rupee,dollar],comparison_currency='INR')
    assert result['categories']['cheapest']['flight']['flight_number']=='EK101'
    assert dollar.price==10 and dollar.currency=='USD'
    assert dollar.comparison_amount=='900.00' and dollar.fx_status=='converted'
    assert dollar.fx_rate_source=='ECB test'

def test_fx_failure_keeps_same_currency_fares_rankable(monkeypatch):
    def fx(amount, source, target):
        if source!='INR': raise FXUnavailable('rate stale')
        return Decimal(str(amount)),FXRate(Decimal(1),source,target,'identity','2026-11-14','now','not_required')
    monkeypatch.setattr(recommendation_engine,'convert',fx)
    result=generate_recommendations([fare('AI101',500,currency='INR'),fare('EK101',1,currency='USD')],comparison_currency='INR')
    assert result['categories']['cheapest']['flight']['flight_number']=='AI101'
    assert result['incomparable_fares']==['EK101']

def test_recommendations_reuse_search_normalization_without_second_fx_call(monkeypatch):
    normalized=fare('EK101',10,currency='USD',comparison_amount='900.00',comparison_currency='INR',fx_status='converted',fx_rate_source='European Central Bank',fx_observation_date='2026-11-13')
    monkeypatch.setattr(recommendation_engine,'convert',lambda *a,**k: (_ for _ in ()).throw(AssertionError('should reuse backend-normalized result')))
    result=generate_recommendations([normalized],comparison_currency='INR')
    assert result['categories']['cheapest']['value']=='900.00'

def test_same_currency_comparison_is_identity_without_rate_observation(monkeypatch):
    monkeypatch.setattr(recommendation_engine,'convert',lambda amount,source,target:(Decimal(str(amount)),FXRate(Decimal(1),source,target,'identity','2026-11-14','now','not_required')))
    candidate=fare('AI101',500)
    generate_recommendations([candidate],comparison_currency='INR')
    assert candidate.fx_status=='same_currency'
    assert candidate.fx_rate_source=='identity'
    assert candidate.fx_observation_date is None

def test_strongest_evidence_quality_returns_correct_candidate():
    weak=fare('AI101',800,departure_at=None,arrival_at=None,schedule_matched=False,retrieved_at=None)
    strong=fare('AI202',900,departure_at='2026-12-20T10:00:00+05:30',arrival_at='2026-12-20T12:00:00+05:30',schedule_matched=True)
    result=generate_recommendations([weak,strong])
    chosen=result['categories']['strongest_evidence_quality']
    assert chosen['flight']['flight_number']=='AI202'
    assert chosen['value']==analyze_evidence(strong)['evidence_score']
    assert chosen['explanation']

def test_equal_evidence_scores_are_reported_as_ties():
    one=fare('AI101',1000)
    two=fare('AI202',1100)
    result=generate_recommendations([one,two])
    selected=result['categories']['strongest_evidence_quality']
    assert selected['tie'] is True
    assert {c['flight_number'] for c in selected['candidates']}=={'AI101','AI202'}

def test_fx_failure_does_not_hide_other_candidates_and_route_level_is_not_ranked(monkeypatch):
    def fx(amount, source, target):
        if source=='USD': raise FXUnavailable('ECB rate is stale')
        return Decimal(str(amount)),FXRate(Decimal(1),source,target,'identity','2026-11-14','now','not_required')
    monkeypatch.setattr(recommendation_engine,'convert',fx)
    good=fare('AI101',500)
    stale=fare('EK101',1,currency='USD')
    route=fare('ROUTE',100,association_level='route_level')
    result=generate_recommendations([good,stale,route])
    assert result['categories']['cheapest']['flight']['flight_number']=='AI101'
    assert 'EK101' in result['incomparable_fares']
    assert stale.fx_status=='stale'
    assert 'ROUTE' not in [x['flight_number'] for x in result['categories']['cheapest']['candidates']]

def test_selected_comparison_currency_changes_normalized_cheapest(monkeypatch):
    def fx(amount, source, target):
        if source==target:
            return Decimal(str(amount)),FXRate(Decimal(1),source,target,'identity','2026-11-14','now','not_required')
        rates={'INR':Decimal('0.01'),'USD':Decimal('0.9'),'EUR':Decimal('1')}
        rate=rates[source]/rates[target]
        return (Decimal(str(amount))*rate).quantize(Decimal('0.01')),FXRate(rate,source,target,'ECB test','2026-11-13','now','fresh')
    monkeypatch.setattr(recommendation_engine,'convert',fx)
    rupee=fare('AI101',1000,currency='INR')
    dollar=fare('EK101',10,currency='USD')
    inr=generate_recommendations([rupee,dollar],comparison_currency='INR')
    inr_amount=dollar.comparison_amount
    eur=generate_recommendations([rupee,dollar],comparison_currency='EUR')
    assert inr['comparison_currency']=='INR' and eur['comparison_currency']=='EUR'
    assert inr['categories']['cheapest']['flight']['flight_number']=='EK101'
    assert eur['categories']['cheapest']['flight']['flight_number']=='EK101'
    assert inr_amount=='900.00'
    assert dollar.comparison_amount=='9.00' and dollar.comparison_currency=='EUR'

def test_fx_incomparable_fare_can_still_participate_in_non_price_categories(monkeypatch):
    monkeypatch.setattr(recommendation_engine,'convert',lambda *a,**k: (_ for _ in ()).throw(FXUnavailable('ECB timeout')))
    timed=fare('EK101',10,currency='USD',departure_at='2026-12-20T10:00:00+05:30',arrival_at='2026-12-20T12:00:00+05:30')
    result=generate_recommendations([timed],comparison_currency='INR')
    assert result['categories']['fastest']['flight']['flight_number']=='EK101'
    assert result['categories']['strongest_evidence_quality']['flight']['flight_number']=='EK101'
    assert 'cheapest' not in result['categories']
