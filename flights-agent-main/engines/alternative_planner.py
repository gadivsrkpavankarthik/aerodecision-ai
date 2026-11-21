"""Only returns distinct alternatives already present in provider results."""
def generate_alternatives(fares, limit=3, exclude_flight_numbers=None):
    excluded=set(exclude_flight_numbers or []); seen=set(); results=[]
    for fare in fares:
        key=(fare.flight_number,fare.origin,fare.destination,fare.travel_date)
        if fare.flight_number in excluded or key in seen or fare.price is None or not fare.price_available: continue
        seen.add(key); results.append({'itinerary':fare.model_dump(mode='json'),'reason':'Provider returned this distinct fare; route and price evidence are retained.'})
        if len(results)>=min(3,max(0,limit)): break
    return results
