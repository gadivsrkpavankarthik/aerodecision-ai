"""Result-quality evaluator: readiness of evidence behind each recommendation.

This does NOT replace the verification engine. It composes existing
verification, freshness, FX comparability, match confidence, and connection
risk signals into a single readiness verdict per candidate.

Statuses: READY / PARTIALLY_VERIFIED / INSUFFICIENT_EVIDENCE.
READY never implies guaranteed booking or seat availability.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from engines.transfer_risk_engine import analyze_transfer


def _facet(fare, name: str) -> str:
    verification = getattr(fare, "verification", None)
    return getattr(verification, name, "UNKNOWN") if verification else "UNKNOWN"


def _freshness(fare) -> dict:
    verification = getattr(fare, "verification", None)
    return {
        "fare": getattr(verification, "freshness_status", "UNKNOWN") if verification else "UNKNOWN",
        "schedule": getattr(verification, "freshness_status", "UNKNOWN") if verification else "UNKNOWN",
        "overall": getattr(verification, "freshness_status", "UNKNOWN") if verification else "UNKNOWN",
    }


def evaluate_result_quality(
    fare,
    provider_statuses: Optional[dict] = None,
    comparison_currency: Optional[str] = None,
) -> dict:
    """Evaluate evidence quality for one candidate."""
    missing: list[str] = []
    expired: list[str] = []
    reasons: list[str] = []

    fare_v = _facet(fare, "fare")
    schedule_v = _facet(fare, "schedule")
    seats_v = _facet(fare, "seats")
    booking_v = _facet(fare, "booking")
    freshness = _freshness(fare)

    # 1-2. Fare/schedule freshness
    verification = getattr(fare, "verification", None)
    if verification is not None:
        for facet in ("fare", "schedule", "seats", "booking"):
            status = getattr(verification, facet, "UNKNOWN")
            if status == "VERIFIED" and freshness["overall"] == "STALE":
                expired.append(facet)
    if fare_v == "VERIFIED" and freshness["overall"] in {"STALE", "UNKNOWN"}:
        missing.append("fare_freshness")
    if schedule_v == "VERIFIED" and freshness["overall"] in {"STALE", "UNKNOWN"}:
        missing.append("schedule_freshness")

    # 3. Seat verification
    if seats_v != "VERIFIED":
        missing.append("seat_availability")

    # 4. Booking-link status
    booking_state = getattr(fare, "booking_state", None) or "BOOKABILITY_UNCONFIRMED"
    if booking_state in {"NO_LINK", "LINK_UNREACHABLE", "BOOKABILITY_UNCONFIRMED"}:
        missing.append("booking_confirmation")

    # 5. Currency comparability
    fx_status = getattr(fare, "fx_status", "same_currency")
    currency_ok = (
        fare.price is None
        or not fare.currency
        or (comparison_currency and (fare.currency.upper() == comparison_currency or fx_status == "converted"))
        or (not comparison_currency and bool(fare.currency))
    )
    if not currency_ok:
        missing.append("currency_comparability")

    # 6. Provider completeness
    provider_statuses = provider_statuses or {}
    provider_failures = [
        name for name, status in provider_statuses.items()
        if status in {"error", "malformed_response", "provider_unavailable", "unavailable"}
    ]
    provider_completeness = "complete" if not provider_failures else "partial" if any(
        s in {"success", "ok", "partial_response"} for s in provider_statuses.values()
    ) else "unavailable"
    if provider_failures:
        missing.append("provider_completeness:" + ",".join(provider_failures))

    # 7. Matching confidence
    match_confidence = getattr(fare, "match_confidence", None) or (
        "EXACT_MATCH" if fare.schedule_matched else "NO_MATCH"
    )
    if match_confidence in {"PARTIAL_MATCH", "NO_MATCH"} and fare.schedule_matched:
        missing.append("match_confidence")

    # 8. Missing critical flight information
    critical_missing = []
    for field_name in ("departure_time", "arrival_time", "price", "currency"):
        if getattr(fare, field_name, None) in (None, ""):
            critical_missing.append(field_name)
    if critical_missing:
        missing.append("critical_fields:" + ",".join(critical_missing))

    # 9. Connection risk
    connection_risk = analyze_transfer(fare) if fare.itinerary_kind == "connecting_self_transfer" else None
    risk_level = (connection_risk or {}).get("risk_level", "UNKNOWN")
    if risk_level in {"HIGH", "UNKNOWN"}:
        missing.append("connection_risk:" + risk_level)

    # 10. Ranking-data completeness
    breakdown = getattr(fare, "score_breakdown", None) or {}
    missing_factors = breakdown.get("missing_factors") or []
    if missing_factors:
        reasons.append("Ranking excluded factors with missing data: " + ", ".join(missing_factors))

    # Verdict
    critical_blockers = [
        m for m in missing
        if m.startswith(("currency_comparability", "provider_completeness", "connection_risk"))
        or m in {"fare_freshness", "schedule_freshness", "critical_fields:"}
    ]
    hard_missing = [m for m in missing if m.split(":")[0] in {
        "currency_comparability", "provider_completeness", "connection_risk",
        "match_confidence", "critical_fields",
    }]
    if fare.price is None or fare_v in {"NOT CONFIRMED", "UNKNOWN"}:
        status = "INSUFFICIENT_EVIDENCE"
    elif expired or (fare_v != "VERIFIED" and schedule_v != "VERIFIED"):
        status = "PARTIALLY_VERIFIED"
    elif seats_v == "VERIFIED" and booking_state == "LINK_AVAILABLE" and not expired:
        status = "READY"
    elif fare_v == "VERIFIED" and schedule_v == "VERIFIED":
        status = "READY" if seats_v == "VERIFIED" else "PARTIALLY_VERIFIED"
    else:
        status = "PARTIALLY_VERIFIED"

    if status == "READY":
        reasons.insert(0, "Fare and schedule evidence verified and fresh; seat and booking states reported independently.")
    elif status == "PARTIALLY_VERIFIED":
        reasons.insert(0, "Core fare/schedule evidence exists but one or more facets are unverified, stale, or incomplete.")
    else:
        reasons.insert(0, "Insufficient evidence to support confident recommendation.")

    return {
        "readiness_status": status,
        "readiness_reasons": reasons,
        "missing_evidence": missing,
        "expired_evidence": expired,
        "provider_completeness": provider_completeness,
        "matching_confidence": match_confidence,
        "connection_risk": risk_level,
        "connection_risk_detail": connection_risk,
        "booking_state": booking_state,
        "freshness": freshness,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "caveat": "READY describes evidence completeness only; it does not guarantee booking, seat availability, or final price.",
    }
