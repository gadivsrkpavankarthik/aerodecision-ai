"""Deterministic recommendations. Prices compare only within a currency; no FX is inferred."""
from decimal import Decimal
from datetime import datetime, timezone
from engines.evidence_engine import analyze_evidence
from engines.transfer_risk_engine import analyze_transfer
from services.fx_service import convert, FXUnavailable
from config import config

def duration_minutes(fare):
    """Return duration only from valid timestamp pairs with explicit offsets."""
    if not fare.departure_at or not fare.arrival_at: return None
    try:
        dep=datetime.fromisoformat(fare.departure_at.replace('Z','+00:00'))
        arr=datetime.fromisoformat(fare.arrival_at.replace('Z','+00:00'))
        if dep.tzinfo is None or arr.tzinfo is None: return None
        minutes=int((arr.astimezone(timezone.utc)-dep.astimezone(timezone.utc)).total_seconds()//60)
        return minutes if 0 < minutes <= 72*60 else None
    except (TypeError,ValueError,OverflowError): return None

def _entry(name, fare, metric, value, reason, tied=None):
    evidence=analyze_evidence(fare)
    return {'flight':fare.model_dump(mode='json'),'candidates':[x.model_dump(mode='json') for x in (tied or [fare])], 'metric':metric,'value':value,'currency':fare.currency,'evidence_score':evidence['evidence_score'],'schedule_status':fare.trust_status.get('schedule','SCHEDULE UNCONFIRMED'),'missing_data':evidence['missing_fields']+['baggage allowance','terminal details'],'explanation':reason,'label':name}

def generate_recommendations(fares, max_budget=None, prohibit_self_transfer=False, comparison_currency=None):
    eligible=[f for f in fares if f.association_level in ('flight_specific','itinerary_specific') and (not prohibit_self_transfer or f.itinerary_kind!='connecting_self_transfer') and f.price_available and f.price is not None and Decimal(str(f.price))>0 and f.currency]
    all_eligible=list(eligible)
    currency=(comparison_currency or config.default_comparison_currency).upper()
    comparable=[]; incomparable=[]; normalized={}
    for fare in eligible:
        if fare.comparison_currency==currency and fare.fx_status in ('same_currency','converted') and fare.comparison_amount is not None:
            try:
                existing=Decimal(str(fare.comparison_amount))
                if existing.is_finite() and existing>=0:
                    normalized[id(fare)]=existing; comparable.append(fare); continue
            except (ValueError,ArithmeticError):
                pass
        try:
            amount,rate=convert(fare.price,fare.currency,currency)
            fare.comparison_amount=str(amount); fare.comparison_currency=currency
            fare.fx_status='same_currency' if fare.currency.upper()==currency else 'converted'
            fare.fx_rate_source=rate.source; fare.fx_observation_date=rate.observation_date if fare.fx_status=='converted' else None; fare.fx_retrieved_at=rate.retrieved_at
            normalized[id(fare)]=amount; comparable.append(fare)
        except FXUnavailable as exc:
            fare.comparison_amount=None
            fare.fx_rate_source=None; fare.fx_observation_date=None; fare.fx_retrieved_at=None
            message=str(exc).lower()
            fare.fx_status='stale' if 'stale' in message else 'unsupported' if any(x in message for x in ('no reference rate','unsupported')) else 'unavailable'
            fare.comparison_currency=currency; incomparable.append(fare.flight_number)
    if max_budget is not None:
        budget_value=Decimal(str(max_budget))
        eligible=[f for f in comparable if normalized[id(f)]<=budget_value]
        comparable=[f for f in eligible]
    else:
        eligible=all_eligible
    result={'categories':{},'pareto_fares':[],'excluded_count':len(fares)-len(all_eligible),'incomparable_fares':incomparable,'comparison_currency':currency,'fx_disclaimer':'Converted amounts use ECB reference exchange rates for comparison. Actual booking charges may differ due to provider pricing, card conversion rates, and fees.'}
    if not eligible: return result
    fare_comparable=[f for f in comparable if max_budget is None or normalized[id(f)]<=Decimal(str(max_budget))]
    if fare_comparable:
        minimum=min(normalized[id(f)] for f in fare_comparable)
        cheapest=[f for f in fare_comparable if normalized[id(f)]==minimum]
        result['categories']['cheapest']={'flight':cheapest[0].model_dump(mode='json'),'candidates':[f.model_dump(mode='json') for f in cheapest],'metric':'ECB-normalized provider fare','value':str(minimum),'currency':currency,'tie':len(cheapest)>1,'evidence_score':max(analyze_evidence(f)['evidence_score'] for f in cheapest),'schedule_status':'SCHEDULE UNCONFIRMED' if not all(f.schedule_matched for f in cheapest) else 'SCHEDULE CONFIRMED','missing_data':['baggage allowance','complete trip cost'],'explanation':f'{len(cheapest)} candidate(s) share the lowest comparable fare in {currency}; source fares remain unchanged.'}
    timed=[f for f in eligible if duration_minutes(f) is not None]
    if timed:
        shortest=min(duration_minutes(f) for f in timed); tied=[f for f in timed if duration_minutes(f)==shortest]
        matched=all(f.schedule_matched for f in tied)
        result['categories']['fastest']={'flight':tied[0].model_dump(mode='json'),'candidates':[f.model_dump(mode='json') for f in tied],'label':'FASTEST MATCHED ITINERARY' if matched else 'FASTEST PROVIDER-REPORTED OPTION','metric':'provider-reported elapsed duration','value':shortest,'unit':'minutes','tie':len(tied)>1,'evidence_score':max(analyze_evidence(f)['evidence_score'] for f in tied),'schedule_status':'SCHEDULE CONFIRMED' if matched else 'SCHEDULE UNCONFIRMED','missing_data':[],'explanation':'Ranking uses provider-reported flight timings. Independent schedule confirmation is unavailable.' if not matched else 'Schedule-matched candidates with the shortest valid elapsed duration.'}
    else: result['categories']['fastest']={'flight':None,'candidates':[],'label':'INSUFFICIENT DATA TO RANK','metric':'provider-reported elapsed duration','value':None,'explanation':'No candidate has a valid timezone-aware departure and arrival timestamp.'}
    # Value uses only comparable fare, valid duration, and evidence. It never assumes complete cost or low risk.
    value_pool=[f for f in timed if f in fare_comparable]
    if value_pool:
        costs=[normalized[id(f)] for f in value_pool]; durations=[duration_minutes(f) for f in value_pool]
        lo_c,hi_c=min(costs),max(costs); lo_d,hi_d=min(durations),max(durations)
        def score(f):
            cs=1.0 if hi_c==lo_c else float((hi_c-normalized[id(f)])/(hi_c-lo_c))
            ds=1.0 if hi_d==lo_d else (hi_d-duration_minutes(f))/(hi_d-lo_d)
            es=analyze_evidence(f)['evidence_score']/100
            return round((cs*.4+ds*.4+es*.2)*100,2)
        winner=max(value_pool,key=lambda f:(score(f),f.flight_number))
        result['categories']['best_overall_value']=_entry('BEST OVERALL VALUE',winner,'documented normalized score',score(winner),'Score = 40% fare + 40% duration + 20% evidence quality. Complete cost and connection safety are not assumed.')
        result['categories']['best_overall_value']['score_breakdown']={'fare_weight':.4,'duration_weight':.4,'evidence_weight':.2,'score':score(winner)}
    transfers=[f for f in eligible if f.itinerary_kind=='connecting_self_transfer']
    assessed=[f for f in transfers if (lambda r:r['risk_score'] is not None and not r['unknowns'])(analyze_transfer(f))]
    if not transfers:
        result['categories']['lowest_assessed_connection_risk']={'flight':None,'candidates':[],'label':'NOT APPLICABLE','metric':'assessed risk','value':None,'explanation':'No self-transfer itineraries are present in the eligible results.'}
    elif not assessed:
        result['categories']['lowest_assessed_connection_risk']={'flight':None,'candidates':[],'label':'INSUFFICIENT DATA TO RANK','metric':'assessed risk','value':None,'missing_data':['baggage','terminal','immigration/transit','onward recovery']}
    else:
        lowest=min(assessed,key=lambda f:analyze_transfer(f)['risk_score'])
        result['categories']['lowest_assessed_connection_risk']={'flight':lowest.model_dump(mode='json'),'candidates':[],'label':'LOWEST ASSESSED CONNECTION RISK','metric':'risk score','value':analyze_transfer(lowest)['risk_score']}
    high=max(analyze_evidence(f)['evidence_score'] for f in eligible); strongest=[f for f in eligible if analyze_evidence(f)['evidence_score']==high]
    result['categories']['strongest_evidence_quality']={'flight':strongest[0].model_dump(mode='json'),'candidates':[f.model_dump(mode='json') for f in strongest],'metric':'evidence score','value':high,'evidence_score':high,'tie':len(strongest)>1,'explanation':f'Highest backend evidence score among eligible candidates; {len(strongest)} candidate(s) tied.'}
    return result
