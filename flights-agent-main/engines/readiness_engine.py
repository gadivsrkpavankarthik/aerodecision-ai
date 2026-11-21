"""Reusable preference checks and evidence-aware readiness checklist."""
def check_readiness(fare, profile=None, evidence=None, risk=None, cost=None):
    profile=profile or {}; checks=[]; failures=[]
    checks.append(('budget',fare.price is not None and (profile.get('maximum_budget') is None or fare.price<=profile['maximum_budget'])))
    checks.append(('maximum_stops',profile.get('maximum_stops') is None or fare.stops<=profile['maximum_stops']))
    checks.append(('self_transfer',not profile.get('no_self_transfer') or fare.itinerary_kind!='connecting_self_transfer'))
    checks.append(('schedule_evidence',bool(fare.schedule_matched)))
    checks.append(('fare_evidence',bool(fare.price_available)))
    checks.append(('booking_link_reviewed',bool(fare.booking_url)))
    checks.append(('baggage_requirements_reviewed',False)); checks.append(('travel_documents_reviewed',False))
    checks.append(('total_cost_reviewed',cost is not None)); checks.append(('backup_available',False))
    failures=[name for name,ok in checks if not ok]
    return {'ready':not failures,'requirements':dict(checks),'failed_or_unconfirmed':failures,'checklist':[{'item':name,'status':'satisfied' if ok else 'failed' if name in {'budget','maximum_stops','self_transfer'} else 'unknown'} for name,ok in checks],'limitations':['Travel-document requirements need current authoritative sources and are not inferred.']}
