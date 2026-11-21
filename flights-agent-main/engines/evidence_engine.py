"""Explainable evidence quality assessment; score is not booking probability."""
from datetime import datetime, timezone
from models import FareResult

DEFAULT_WEIGHTS = {'schedule_match': 30, 'fare_response': 20, 'flight_number': 10, 'route': 10, 'date': 10, 'fresh_fare': 10, 'booking_link_check': 10}

def analyze_evidence(fare: FareResult, stale_after_minutes: int = 60, weights: dict | None = None) -> dict:
    missing, reasons, conflicts = [], [], []
    checks = {}
    checks['schedule_match_status'] = 'matched' if fare.schedule_matched else 'unmatched'
    checks['airline_match_status'] = 'matched' if fare.airline or fare.airline_code else 'unknown'
    checks['route_match_status'] = 'available' if fare.origin and fare.destination else 'missing'
    checks['date_match_status'] = 'available' if fare.travel_date else 'missing'
    checks['flight_number_match_status'] = 'available' if fare.flight_number else 'missing'
    if not fare.schedule_matched: reasons.append('Fare is not matched to a returned schedule.')
    if not fare.airline and not fare.airline_code: missing.append('airline')
    if not fare.departure_time: missing.append('departure_time')
    if fare.price is None or not fare.currency: missing.append('fare_amount_or_currency')
    if fare.price_status in {'unavailable','error'}: reasons.append('Provider has no usable fare quote.')
    if fare.availability_status != 'provider_confirmed': reasons.append('Seat availability is not confirmed by the provider.')
    retrieved = None
    if fare.retrieved_at:
        try:
            retrieved = datetime.fromisoformat(fare.retrieved_at.replace('Z','+00:00'))
            if retrieved.tzinfo is None: retrieved = retrieved.replace(tzinfo=timezone.utc)
        except ValueError: missing.append('valid_retrieval_timestamp')
    age = (datetime.now(timezone.utc)-retrieved.astimezone(timezone.utc)).total_seconds()/60 if retrieved else None
    freshness = 'unknown' if age is None else ('stale' if age > stale_after_minutes else 'fresh')
    if freshness == 'stale': reasons.append('Fare evidence is older than the configured freshness window.')
    if missing: reasons.append('Evidence is incomplete: ' + ', '.join(missing) + '.')
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    awarded = {'schedule_match': weights['schedule_match'] if fare.schedule_matched else 0,
        'fare_response': weights['fare_response'] if fare.price is not None and fare.currency and fare.source else 0,
        'flight_number': weights['flight_number'] if fare.flight_number else 0,
        'route': weights['route'] if fare.origin and fare.destination else 0,
        'date': weights['date'] if fare.travel_date else 0,
        'fresh_fare': weights['fresh_fare'] if freshness == 'fresh' else 0,
        'booking_link_check': weights['booking_link_check'] if fare.web_verification_status in {'passed','partial'} else 0}
    score = round(sum(awarded.values()) / max(1, sum(weights.values())) * 100)
    status = 'conflicting' if conflicts else 'stale' if freshness == 'stale' else 'incomplete' if missing else 'verified_against_available_evidence' if fare.schedule_matched and fare.price_status == 'verified' else 'partial'
    return {'schedule_source': fare.schedule_source or ('AviationStack' if fare.schedule_matched else None), 'fare_source': fare.fare_source or fare.source, 'retrieved_at': fare.retrieved_at, **checks, 'fare_freshness': freshness, 'booking_link_status': fare.web_verification_status if fare.booking_url else 'unavailable', 'missing_fields': missing, 'conflicting_fields': conflicts, 'evidence_score': score, 'score_breakdown': awarded, 'score_weights': weights, 'evidence_status': status, 'evidence_reasons': reasons + ['Score reflects only available data evidence; it is not a safety or booking probability.']}
