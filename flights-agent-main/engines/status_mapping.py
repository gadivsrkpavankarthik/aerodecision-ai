"""Shared backend status terminology for API, traces, tables, and saved records."""

def candidate_status(fare):
    if fare.itinerary_kind == 'connecting_self_transfer':
        return {'fare':'FARES RECEIVED','schedule':'LEG SCHEDULES UNCONFIRMED' if not fare.schedule_matched else 'LEG SCHEDULES MATCHED','overall':'SELF-TRANSFER ESTIMATE','availability':'SEAT AVAILABILITY NOT CONFIRMED'}
    if not fare.price_available or fare.price is None:
        return {'fare':'FARE UNAVAILABLE','schedule':'SCHEDULE ONLY','overall':'SCHEDULE ONLY','availability':'SEAT AVAILABILITY NOT CONFIRMED'}
    if fare.schedule_matched:
        return {'fare':'FARE RECEIVED','schedule':'SCHEDULE CONFIRMED','overall':'MATCHED FARE AND SCHEDULE','availability':'SEAT AVAILABILITY NOT CONFIRMED' if fare.availability_status!='provider_confirmed' else 'PROVIDER CONFIRMED AVAILABILITY'}
    return {'fare':'FARE RECEIVED','schedule':'SCHEDULE UNCONFIRMED','overall':'FARE ONLY','availability':'SEAT AVAILABILITY NOT CONFIRMED'}

def execution_status(provider_statuses, schedules, fares, candidates):
    statuses=list((provider_statuses or {}).values())
    success=any(s in {'success','ok','partial_response'} for s in statuses)
    failure=any(s in {'error','malformed_response','provider_unavailable','future_date_unavailable','unavailable'} for s in statuses)
    if not success and failure: return 'PROVIDER UNAVAILABLE'
    if success and failure: return 'COMPLETED WITH PARTIAL DATA'
    if not schedules and not fares and not candidates: return 'NO RESULTS'
    if schedules or fares: return 'COMPLETED'
    return 'FAILED'
