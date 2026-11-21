"""Flight Intelligence analytics — every metric computed from real stored records.

Data sources:
- searches.record_json  : per-search summary (counts, statuses, best flight)
- searches.fares_json   : per-fare snapshots (price, verification, score, risk)
- provider_events       : per-provider call telemetry
- interaction_events    : real user interactions (clicks, not bookings)

Honesty rules: no fabricated values; undefined metrics return value=None with
availability='unavailable' and a reason. Percentages always carry denominators.
"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

FAILURE_STATUSES = {"error", "malformed_response", "provider_unavailable", "unavailable", "timeout", "future_date_unavailable"}
PARTIAL_STATUSES = {"partial_response"}
SUCCESS_STATUSES = {"success"}


def _metric(value: Any, unit: str, sample_size: int, source: str, **extra) -> dict:
    return {"value": value, "unit": unit, "sample_size": sample_size,
            "source": source, "availability": "available" if value is not None else "unavailable", **extra}


def _unavailable(reason: str, source: str) -> dict:
    return {"value": None, "unit": None, "sample_size": 0, "source": source,
            "availability": "unavailable", "reason": reason}


def _eligible_fares(searches: list[dict]) -> list[dict]:
    """All fare snapshots with constraint_status eligible (or unset = eligible)."""
    out = []
    for s in searches:
        for f in s.get("fares") or []:
            if f.get("constraint_status") in (None, "eligible") and f.get("price") is not None:
                out.append(f)
    return out


def fare_intelligence(searches: list[dict]) -> dict:
    fares = _eligible_fares(searches)
    n = len(fares)
    if not n:
        return {"status": "no_data", "reason": "No eligible fare records stored yet.",
                "metrics": {}, "sample_size": 0}
    # Use comparison_amount when FX-converted, else native price — never mix currencies.
    priced = [f for f in fares if f.get("comparison_amount") is not None]
    amounts = [float(f["comparison_amount"]) for f in priced]
    currency = priced[0].get("comparison_currency") if priced else None
    budgets = [s.get("budget") for s in searches if s.get("budget") is not None]
    within = sum(1 for f in priced if f.get("comparison_amount") is not None)
    metrics = {
        "cheapest": _metric(min(amounts) if amounts else None, currency, len(amounts), "searches.fares_json"),
        "highest": _metric(max(amounts) if amounts else None, currency, len(amounts), "searches.fares_json"),
        "average": _metric(round(statistics.mean(amounts), 2) if amounts else None, currency, len(amounts), "searches.fares_json"),
        "median": _metric(round(statistics.median(amounts), 2) if amounts else None, currency, len(amounts), "searches.fares_json"),
        "eligible_count": _metric(n, "fares", n, "searches.fares_json"),
        "currency": _metric(currency, "ISO4217", len(amounts), "searches.fares_json"),
        "fx_converted_count": _metric(len(priced), "fares", n, "searches.fares_json",
                                    note="Fares without FX conversion excluded from currency-mixed stats"),
        "searches_with_budget": _metric(len(budgets), "searches", len(searches), "searches.record_json"),
    }
    # Budget compliance: only when budget currency matches fare comparison currency.
    compliant = incomparable = 0
    for s in searches:
        budget = s.get("budget")
        bcur = (s.get("budget_currency") or "").upper()
        for f in s.get("fares") or []:
            if f.get("constraint_status") not in (None, "eligible") or f.get("price") is None:
                continue
            if budget is None:
                continue
            amt = f.get("comparison_amount")
            cur = (f.get("comparison_currency") or "").upper()
            if amt is None or not cur or cur != bcur:
                incomparable += 1
                continue
            if float(amt) <= float(budget):
                compliant += 1
            else:
                incomparable += 1
    total_budgeted = compliant + incomparable
    metrics["within_budget"] = _metric(compliant, "fares", total_budgeted, "searches.fares_json",
                                     note=f"{incomparable} excluded: currency incomparable or no FX conversion")
    metrics["outside_budget"] = _metric(incomparable, "fares", total_budgeted, "searches.fares_json")
    # Airline-wise average (comparison currency only)
    by_airline: dict[str, list[float]] = {}
    for f in priced:
        by_airline.setdefault(f.get("airline") or f.get("airline_code") or "Unknown", []).append(float(f["comparison_amount"]))
    metrics["airline_averages"] = {
        k: {"average": round(statistics.mean(v), 2), "count": len(v)}
        for k, v in sorted(by_airline.items(), key=lambda kv: statistics.mean(kv[1]))
    }
    return {"status": "ok", "metrics": metrics, "sample_size": n,
            "note": "Single-search fare distribution, not a historical price trend."}


def journey_efficiency(searches: list[dict]) -> dict:
    """Duration only from tz-aware departure_at/arrival_at; HH:MM-only excluded & counted."""
    from datetime import datetime
    durations, excluded_naive, excluded_missing = [], 0, 0
    direct = connecting = 0
    layovers = []
    for s in searches:
        for f in s.get("fares") or []:
            if f.get("constraint_status") not in (None, "eligible"):
                continue
            if f.get("itinerary_kind") == "connecting_self_transfer":
                connecting += 1
            else:
                direct += 1
            dep, arr = f.get("departure_at"), f.get("arrival_at")
            if not dep or not arr:
                excluded_missing += 1
                continue
            try:
                d1 = datetime.fromisoformat(str(dep).replace("Z", "+00:00"))
                d2 = datetime.fromisoformat(str(arr).replace("Z", "+00:00"))
                if d1.tzinfo is None or d2.tzinfo is None:
                    excluded_naive += 1
                    continue
                minutes = (d2 - d1).total_seconds() / 60
                if minutes > 0:
                    durations.append(minutes)
            except (ValueError, TypeError):
                excluded_naive += 1
    n = len(durations)
    if not n:
        return {"status": "no_data",
                "reason": f"No timezone-aware departure/arrival timestamps in stored fares ({excluded_naive} naive, {excluded_missing} missing).",
                "excluded": {"naive_or_invalid": excluded_naive, "missing": excluded_missing},
                "sample_size": 0}
    return {"status": "ok",
            "metrics": {
                "shortest_minutes": _metric(round(min(durations)), "minutes", n, "searches.fares_json"),
                "average_minutes": _metric(round(statistics.mean(durations)), "minutes", n, "searches.fares_json"),
                "longest_minutes": _metric(round(max(durations)), "minutes", n, "searches.fares_json"),
                "direct_count": _metric(direct, "fares", direct + connecting, "searches.fares_json"),
                "connecting_count": _metric(connecting, "fares", direct + connecting, "searches.fares_json"),
            },
            "excluded": {"naive_or_invalid": excluded_naive, "missing": excluded_missing},
            "sample_size": n}


def recommendation_insights(searches: list[dict]) -> dict:
    """Reuse stored ranking output — never recompute a second ranking."""
    best_scores, cheapest_scores = [], []
    cheapest_vs_best = []
    for s in searches:
        fares = [f for f in (s.get("fares") or []) if f.get("constraint_status") in (None, "eligible")]
        scored = [f for f in fares if f.get("score") is not None]
        if scored:
            top = max(scored, key=lambda f: f["score"])
            best_scores.append({"score": top["score"], "flight": top.get("flight_number"),
                                "search_id": s.get("search_id")})
        priced = [f for f in fares if f.get("comparison_amount") is not None]
        if priced:
            cheap = min(priced, key=lambda f: f["comparison_amount"])
            cheapest_scores.append({"price": cheap["comparison_amount"],
                                    "currency": cheap.get("comparison_currency"),
                                    "flight": cheap.get("flight_number")})
            if scored:
                top = max(scored, key=lambda f: f["score"])
                tp = top.get("comparison_amount")
                if tp is not None and cheap.get("comparison_currency") == top.get("comparison_currency"):
                    cheapest_vs_best.append(round(float(tp) - float(cheap["comparison_amount"]), 2))
    if not best_scores and not cheapest_scores:
        return {"status": "no_data", "reason": "No ranked candidates stored yet.", "sample_size": 0}
    return {"status": "ok",
            "metrics": {
                "best_overall_scores": _metric([b["score"] for b in best_scores], "score/100",
                                               len(best_scores), "searches.fares_json.score_breakdown"),
                "cheapest_fares": _metric([c["price"] for c in cheapest_scores], "currency",
                                          len(cheapest_scores), "searches.fares_json"),
                "cheapest_vs_best_delta": _metric(
                    {"average": round(statistics.mean(cheapest_vs_best), 2), "count": len(cheapest_vs_best)}
                    if cheapest_vs_best else None, "currency", len(cheapest_vs_best),
                    "searches.fares_json",
                    note="Positive = best-overall costs more than cheapest"),
            },
            "sample_size": max(len(best_scores), len(cheapest_scores))}


def risk_insights(searches: list[dict]) -> dict:
    counts = Counter()
    reasons = Counter()
    self_transfer = airport_change = overnight = 0
    for s in searches:
        for f in s.get("fares") or []:
            if f.get("constraint_status") not in (None, "eligible"):
                continue
            lvl = f.get("risk_level") or "UNKNOWN"
            counts[lvl] += 1
            if f.get("itinerary_kind") == "connecting_self_transfer":
                self_transfer += 1
    total = sum(counts.values())
    if not total:
        return {"status": "no_data", "reason": "No eligible candidates stored yet.", "sample_size": 0}
    return {"status": "ok",
            "metrics": {
                "risk_distribution": _metric(dict(counts), "fares", total, "searches.fares_json.risk_level"),
                "self_transfer_count": _metric(self_transfer, "fares", total, "searches.fares_json"),
                "not_applicable_count": _metric(counts.get("NOT_APPLICABLE", 0), "fares", total,
                                                "searches.fares_json.risk_level",
                                                note="Confirmed direct flights — risk does not apply"),
                "unknown_count": _metric(counts.get("UNKNOWN", 0), "fares", total,
                                         "searches.fares_json.risk_level",
                                         note="Unconfirmed itinerary structure — not assumed safe"),
            },
            "sample_size": total}


def provider_performance(provider_events: list[dict]) -> dict:
    if not provider_events:
        return {"status": "no_data", "reason": "Not enough telemetry — no provider events recorded.",
                "providers": {}, "sample_size": 0}
    providers: dict[str, list[dict]] = {}
    for e in provider_events:
        providers.setdefault(e.get("provider") or "unknown", []).append(e)
    out = {}
    for name, events in providers.items():
        total = len(events)
        statuses = Counter(e.get("status") or "unknown" for e in events)
        success = sum(statuses[s] for s in SUCCESS_STATUSES)
        failed = sum(statuses[s] for s in FAILURE_STATUSES)
        partial = sum(statuses[s] for s in PARTIAL_STATUSES)
        durations = [e["duration_ms"] for e in events if e.get("duration_ms") is not None]
        out[name] = {
            "total_requests": total,
            "success_requests": success,
            "failed_requests": failed,
            "partial_requests": partial,
            "future_date_unavailable": statuses.get("future_date_unavailable", 0),
            "success_rate_pct": round(success / total * 100, 1) if total else None,
            "failure_rate_pct": round(failed / total * 100, 1) if total else None,
            "partial_rate_pct": round(partial / total * 100, 1) if total else None,
            "avg_latency_ms": round(statistics.mean(durations)) if durations else None,
            "median_latency_ms": round(statistics.median(durations)) if durations else None,
            "max_latency_ms": max(durations) if durations else None,
            "status_distribution": dict(statuses),
            "sample_size": total,
        }
    return {"status": "ok", "providers": out, "sample_size": len(provider_events)}


def matching_performance(searches: list[dict]) -> dict:
    """Search-level funnel from stored counts — never mixes searches."""
    if not searches:
        return {"status": "no_data", "reason": "No searches recorded.", "sample_size": 0}
    totals = Counter()
    for s in searches:
        totals["searches"] += 1
        totals["schedule_records"] += s.get("schedule_count") or 0
        totals["fare_records"] += s.get("fare_count") or 0
        totals["eligible_candidates"] += s.get("eligible_count") or 0
        totals["rejected_candidates"] += s.get("rejected_count") or 0
        totals["matched_schedule_count"] += s.get("matched_schedule_count") or 0
        totals["verified_fares"] += s.get("verified_fare_count") or 0
        totals["unverified_fares"] += s.get("unverified_fare_count") or 0
    # Fare-only = fares without schedule match; schedule-only inferred when schedules>0 & fares==0
    fare_only = sum(
        1 for s in searches for f in (s.get("fares") or [])
        if not f.get("schedule_matched") and f.get("price") is not None
    )
    schedule_only = sum(1 for s in searches if (s.get("schedule_count") or 0) > 0 and (s.get("fare_count") or 0) == 0)
    totals["fare_only_candidates"] = fare_only
    totals["searches_with_schedules_only"] = schedule_only
    return {"status": "ok", "funnel": dict(totals), "sample_size": len(searches),
            "note": "Per-search funnel aggregated; fare-only is never counted as a schedule match."}


def verification_quality(searches: list[dict]) -> dict:
    facets = {"fare": Counter(), "schedule": Counter(), "seats": Counter(), "booking": Counter()}
    freshness = Counter()
    total_fares = 0
    for s in searches:
        for f in s.get("fares") or []:
            total_fares += 1
            v = f.get("verification") or {}
            for facet in facets:
                facets[facet][v.get(facet) or "UNKNOWN"] += 1
            freshness[f.get("freshness") or "UNKNOWN"] += 1
    if not total_fares:
        return {"status": "no_data", "reason": "No fare snapshots stored yet.", "sample_size": 0}
    return {"status": "ok",
            "metrics": {
                "facet_distribution": {k: dict(v) for k, v in facets.items()},
                "freshness_distribution": dict(freshness),
                "total_fares": total_fares,
            },
            "sample_size": total_fares,
            "note": "Facets are independent; a verified fare never implies a verified schedule."}


def decision_engine(searches: list[dict]) -> dict:
    if not searches:
        return {"status": "no_data", "reason": "No searches recorded.", "sample_size": 0}
    outcomes = Counter(s.get("search_outcome") or "unknown" for s in searches)
    readiness = Counter()
    rejections = Counter()
    for s in searches:
        # readiness lives in record_json? Not stored — derive from best_result_quality if present in fares? Use search_outcome + error_code.
        if s.get("error_code"):
            rejections[s["error_code"]] += 1
    # Result-quality readiness is not persisted per search; report what's stored honestly.
    return {"status": "ok",
            "metrics": {
                "search_outcomes": dict(outcomes),
                "error_codes": dict(rejections),
                "total_searches": len(searches),
                "searches_with_best_flight": sum(1 for s in searches if s.get("flight_found")),
                "avg_eligible_per_search": round(
                    sum(s.get("eligible_count") or 0 for s in searches) / len(searches), 2),
            },
            "sample_size": len(searches),
            "note": "Readiness verdicts are computed per-response; only search outcomes are persisted."}


def interaction_analytics(interactions: list[dict]) -> dict:
    if not interactions:
        return {"status": "no_data", "reason": "No interactions recorded yet.", "sample_size": 0}
    counts = Counter(e.get("event_type") or "unknown" for e in interactions)
    return {"status": "ok",
            "metrics": {"event_counts": dict(counts), "total_events": len(interactions)},
            "sample_size": len(interactions),
            "note": "Booking-link clicks are clicks — never counted as bookings."}


def build_insights(searches: list[dict], provider_events: list[dict], interactions: list[dict]) -> dict:
    return {
        "fares": fare_intelligence(searches),
        "journey": journey_efficiency(searches),
        "recommendations": recommendation_insights(searches),
        "risks": risk_insights(searches),
        "providers": provider_performance(provider_events),
        "matching": matching_performance(searches),
        "verification": verification_quality(searches),
        "decision_engine": decision_engine(searches),
        "interactions": interaction_analytics(interactions),
    }
