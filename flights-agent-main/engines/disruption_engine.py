"""Deterministic what-if connection scenarios; not delay predictions."""
def simulate_disruption(fare, delay_minutes=0, cancelled=False):
    if delay_minutes < 0: raise ValueError('Delay must be non-negative.')
    connecting=fare.itinerary_kind=='connecting_self_transfer'
    remaining=(fare.layover_minutes-delay_minutes) if connecting and fare.layover_minutes is not None else None
    feasible=not cancelled and (remaining is None or remaining>0)
    return {'flight_number':fare.flight_number,'scenario':'cancellation' if cancelled else f'{delay_minutes}-minute delay','delay_minutes':delay_minutes,'remaining_connection_buffer_minutes':remaining,'connection_feasible':feasible if connecting else None,'additional_cost':None,'available_recovery_options':[],'limitations':['Scenario only; this does not predict actual delays.','No replacement flights or fares are invented.','Provider-backed recovery options are unavailable unless explicitly supplied.']}
