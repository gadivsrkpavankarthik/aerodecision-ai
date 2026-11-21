"""Compose the deterministic decision engines into the live search pipeline."""
from engines.alternative_planner import generate_alternatives
from engines.disruption_engine import simulate_disruption
from engines.evidence_engine import analyze_evidence
from engines.readiness_engine import check_readiness
from engines.result_quality_evaluator import evaluate_result_quality
from engines.recommendation_engine import generate_recommendations
from engines.transfer_risk_engine import analyze_transfer
from engines.trip_cost_engine import estimate_trip_cost
from engines.status_mapping import candidate_status, execution_status
from config import config


def build_decision_workspace(result):
    candidates = result.valid_candidates or []
    best = result.best_flight
    request = result.search_request
    comparison_currency = request.currency if request and request.budget is not None else config.default_comparison_currency
    all_fares = result.fares_found or []
    for fare in all_fares + (result.valid_candidates or []):
        fare.trust_status = candidate_status(fare)
    result.execution_status = execution_status(result.provider_statuses, result.flights_found, all_fares, candidates)
    return {
        "evidence": {fare.flight_number: analyze_evidence(fare) for fare in all_fares},
        "connection_risk": {fare.flight_number: analyze_transfer(fare) for fare in candidates},
        "cost": estimate_trip_cost(
            best.legs if best and best.legs else ([best] if best else []),
            passengers=request.passengers if request else 1,
        ),
        "recommendations": generate_recommendations(
            candidates,
            max_budget=request.budget if request else None,
            prohibit_self_transfer=False,
            comparison_currency=comparison_currency,
        ),
        "alternatives": generate_alternatives(
            candidates,
            exclude_flight_numbers=[best.flight_number] if best else [],
        ),
        "disruption_scenarios": {
            str(delay): simulate_disruption(best, delay_minutes=delay)
            for delay in (15, 30, 60, 90, 120)
        } if best else {},
        "readiness": check_readiness(best) if best else None,
        "result_quality": {
            fare.flight_number: evaluate_result_quality(
                fare,
                provider_statuses=result.provider_statuses,
                comparison_currency=comparison_currency,
            )
            for fare in candidates
        },
        "best_result_quality": (
            evaluate_result_quality(
                best,
                provider_statuses=result.provider_statuses,
                comparison_currency=comparison_currency,
            )
            if best else None
        ),
        "source_status": dict(result.provider_statuses),
        "comparison_currency": comparison_currency,
        "route_level_fares": list(result.unassociated_fare_records),
        "limitations": [
            "Analysis uses only records returned by the configured providers.",
            "Unknown baggage, airport transfer, accommodation, terminal, immigration, and document requirements remain unresolved.",
            "Disruption scenarios are deterministic what-if calculations, not delay predictions.",
        ],
    }
