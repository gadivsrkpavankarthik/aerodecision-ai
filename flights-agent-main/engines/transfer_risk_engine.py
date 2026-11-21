"""Transparent transfer risk checks from available itinerary evidence."""
def analyze_transfer(fare, min_buffer_minutes=120, max_layover_minutes=720):
    if fare.itinerary_kind != 'connecting_self_transfer' or not fare.legs or len(fare.legs) < 2:
        is_direct = fare.itinerary_kind == 'direct' and bool(getattr(fare, 'schedule_matched', False))
        return {
            'risk_score': None,
            'risk_level': 'NOT_APPLICABLE' if is_direct else 'UNKNOWN',
            'category': 'not_applicable' if is_direct else 'insufficient_data',
            'itinerary_type': 'direct' if is_direct else 'unknown',
            'factors': [],
            'unknowns': (
                ['Disruption, baggage, accessibility, and operational risks are not assessed from this record.']
                if is_direct
                else ['Connection legs are unavailable; transfer feasibility cannot be assessed.']
            ),
            'risk_reasons': (
                [{'code': 'not_a_transfer', 'severity': 'info', 'detail': 'Direct itinerary; transfer risk does not apply.'}]
                if is_direct
                else [{'code': 'insufficient_legs', 'severity': 'high', 'detail': 'Connection legs are unavailable; risk cannot be assessed.'}]
            ),
        }
    first, second = fare.legs[0], fare.legs[1]
    factors = []
    risk_reasons = []
    layover = fare.layover_minutes
    if layover is None: factors.append({'factor':'connection_duration','status':'unknown'})
    elif layover < min_buffer_minutes: factors.append({'factor':'short_separate_ticket_buffer','minutes':layover,'recommended_buffer_minutes':min_buffer_minutes})
    elif layover > max_layover_minutes: factors.append({'factor':'layover_exceeds_preference','minutes':layover})
    factors.append({'factor':'ticket_structure','status':'separate_tickets','detail':'Each leg must be booked separately.'})
    unknowns=['Baggage collection/recheck requirements are unknown.','Terminal and immigration requirements are unknown.','Later onward-flight availability is not established.']
    risk_reasons.append({'code':'separate_tickets','severity':'high','detail':'Each leg must be booked separately; no through-ticket protection applies.'})
    if first.destination.upper() != second.origin.upper():
        risk_reasons.append({'code':'airport_change','severity':'high','detail':f"Legs connect via different airports ({first.destination} -> {second.origin})."})
    # Overnight connection: second leg departs on a later calendar date than first leg arrives.
    if first.arrival_at and second.departure_at:
        try:
            from datetime import datetime
            arr_date = datetime.fromisoformat(first.arrival_at.replace('Z','+00:00')).date()
            dep_date = datetime.fromisoformat(second.departure_at.replace('Z','+00:00')).date()
            if dep_date > arr_date:
                risk_reasons.append({'code':'overnight_connection','severity':'warning','detail':'Connection spans an overnight stay at the hub.'})
        except (ValueError, TypeError):
            pass
    if first.arrival_at and second.departure_at:
        try:
            from datetime import datetime
            arr_off = datetime.fromisoformat(first.arrival_at.replace('Z','+00:00')).utcoffset()
            dep_off = datetime.fromisoformat(second.departure_at.replace('Z','+00:00')).utcoffset()
            if arr_off is not None and dep_off is not None and arr_off != dep_off:
                risk_reasons.append({'code':'timezone_change','severity':'info','detail':'Legs use different timezone offsets; layover shown in provider time.'})
        except (ValueError, TypeError):
            pass
    if layover is not None:
        if layover < min_buffer_minutes:
            risk_reasons.append({'code':'short_layover','severity':'high','detail':f'Layover {layover} min is below the {min_buffer_minutes} min separate-ticket buffer.'})
        elif layover > max_layover_minutes:
            risk_reasons.append({'code':'long_layover','severity':'warning','detail':f'Layover {layover} min exceeds the {max_layover_minutes} min preference.'})
    else:
        risk_reasons.append({'code':'layover_unknown','severity':'high','detail':'Connection duration is unavailable; feasibility cannot be confirmed.'})
    risk_reasons.append({'code':'baggage_recheck_unknown','severity':'warning','detail':'Baggage must be collected and rechecked; requirements are unconfirmed.'})
    risk_reasons.append({'code':'immigration_unknown','severity':'warning','detail':'Immigration/transit requirements are unconfirmed.'})
    risk_reasons.append({'code':'no_missed_connection_protection','severity':'high','detail':'Separate tickets provide no missed-connection protection.'})
    score = min(100, (40 if layover is None or layover < min_buffer_minutes else 15) + 20 + len(unknowns)*8)
    if first.destination != second.origin: score=100; factors.append({'factor':'airport_change','status':'route_discontinuity'})
    if first.destination.upper() != second.origin.upper():
        category='higher_concern'; explanation='The legs do not connect at the same airport.'
        risk_level='HIGH'
    elif layover is None:
        category='insufficient_information'; explanation='Connection duration is unavailable.'
        risk_level='UNKNOWN'
    elif layover < min_buffer_minutes:
        category='higher_concern'; explanation='Connection is shorter than the configured separate-ticket buffer.'
        risk_level='HIGH' if layover < 90 else 'MEDIUM'
    elif layover > max_layover_minutes:
        category='moderate_concern'; explanation='Connection exceeds the configured maximum layover preference.'
        risk_level='MEDIUM'
    else:
        category='moderate_concern'; explanation='Timing is within configured bounds, but separate-ticket and operational risks remain.'
        risk_level='MEDIUM' if layover <= 360 else 'LOW'
    return {'risk_score':score,'risk_level':risk_level,'category':category,'factors':factors,'unknowns':unknowns,'explanation':explanation,'recommended_buffer_minutes':min_buffer_minutes,'risk_reasons':risk_reasons,
            'ticket_structure':'separate_tickets','missed_connection_protection':'none — each leg must be booked separately; no through-ticket protection applies'}
