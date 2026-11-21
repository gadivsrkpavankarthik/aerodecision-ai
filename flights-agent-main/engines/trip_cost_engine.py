"""Itemized deterministic trip cost calculation with explicit unknowns."""
def estimate_trip_cost(fares, expenses=None, passengers=1, currency=None):
    fares = fares if isinstance(fares, list) else [fares]
    expenses = expenses or {}
    currencies={str(f.currency).upper() for f in fares if f.price is not None and f.currency}
    if currency: currencies.add(currency.upper())
    compatible=len(currencies)<=1
    cur=next(iter(currencies),None) if compatible else None
    flight=sum(float(f.price) for f in fares if f.price is not None) if compatible and all(f.price is not None for f in fares) else None
    items=[]
    if flight is not None: items.append({'category':'flight','amount':flight*passengers,'currency':cur,'certainty':'provider_reported'})
    else: items.append({'category':'flight','amount':None,'currency':cur,'certainty':'unknown'})
    estimate=0.0; confirmed=0.0; unknown=[]
    for kind, value in expenses.items():
        if value is None: unknown.append(kind); continue
        amount=float(value)
        if amount < 0: raise ValueError('Expense amounts must be non-negative.')
        items.append({'category':kind,'amount':amount,'currency':cur,'certainty':'user_estimated'})
        estimate += amount
    if flight is not None: confirmed=flight*passengers
    else: unknown.append('flight_total_currency_or_fare')
    unknown.extend(k for k in ('baggage','seat','meal','airport_transfer','accommodation') if k not in expenses)
    known = (flight*passengers if flight is not None else 0) + estimate
    return {'cost_basis':'Provider airfare is actual; user-supplied expenses are estimates; missing items are listed as unknown and no add-ons are invented.','flight_cost':flight*passengers if flight is not None else None,'baggage_cost':expenses.get('baggage'),'seat_cost':expenses.get('seat'),'meal_cost':expenses.get('meal'),'airport_transfer_cost':expenses.get('airport_transfer'),'accommodation_cost':expenses.get('accommodation'),'other_costs':sum(float(v) for k,v in expenses.items() if k not in {'baggage','seat','meal','airport_transfer','accommodation'} and v is not None),'known_cost':known if flight is not None else None,'estimated_total_cost':known if flight is not None and not unknown else None,'complete_trip_cost':known if flight is not None and not unknown else None,'currency':cur,'confirmed_total':confirmed,'estimated_additional_expenses':estimate,'estimated_total':estimate,'unknown_expenses':sorted(set(unknown)),'cost_assumptions':['Known cost includes returned fares and supplied expense amounts only.','Unknown expenses prevent a complete trip total from being stated.','User-entered amounts are estimates unless separately confirmed.'],'items':items}
