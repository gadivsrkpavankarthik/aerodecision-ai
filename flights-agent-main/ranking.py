"""Normalized ranking for eligible flight fares.

The default BEST_BALANCED profile uses an explainable 5-factor weighted score
(price, departure preference, duration, stops, verification) with per-factor
weights configurable via RANKING_*_WEIGHT environment variables. A factor whose
data is missing for a candidate is excluded from that candidate's available
weight instead of being rewarded or penalized. Native provider prices are never
rewritten; the ECB-normalized comparison amount is used when present.
"""

from __future__ import annotations

import re
from typing import Optional

from config import config
from engines.recommendation_engine import duration_minutes
from models import FareResult, SearchRequest


def _parse_departure_hour(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if "am" in text or "pm" in text:
        match = re.search(r"(\d{1,2}):(\d{2})", text)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= minute < 60):
            return None
        suffix = "pm" if "pm" in text else "am"
        if suffix == "pm" and hour != 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        return hour

    try:
        match = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        return hour
    except (TypeError, ValueError):
        return None


def _departure_score(fare: FareResult, preference: Optional[str]) -> Optional[float]:
    """Preferred-departure-time match; None when the factor cannot be evaluated."""
    if not preference:
        return None
    hour = _parse_departure_hour(fare.departure_time)
    if hour is None:
        return None
    matches = {
        "morning": 5 <= hour < 12,
        "afternoon": 12 <= hour < 17,
        "evening": 17 <= hour < 21,
        "night": hour >= 21 or hour < 5,
    }
    normalized_preference = str(preference).strip().casefold()
    return float(matches.get(normalized_preference, False))


def _transfer_concern_score(fare: FareResult) -> float:
    if fare.itinerary_kind != "connecting_self_transfer":
        return 0.0
    layover = fare.layover_minutes or 0
    if fare.connection_hub and fare.connection_hub != fare.destination and fare.connection_hub != fare.origin:
        return 0.5
    if layover < 90:
        return 0.9
    if layover < 180:
        return 0.6
    if layover > 360:
        return 0.4
    return 0.2


def _comparable_price(fare: FareResult) -> Optional[float]:
    """Price normalized into the shared comparison currency when available.

    Native provider prices are never rewritten; ranking uses the ECB-normalized
    comparison amount only when the pipeline already computed one.
    """
    if fare.comparison_amount:
        try:
            value = float(fare.comparison_amount)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            return value
    if fare.price is None:
        return None
    try:
        return float(fare.price)
    except (TypeError, ValueError):
        return None


def _price_or_inf(fare: FareResult) -> float:
    price = _comparable_price(fare)
    return price if price is not None else float("inf")


_VERIFICATION_FACTOR_WEIGHTS = {"fare": 0.5, "schedule": 0.3, "seats": 0.2}
_VERIFICATION_STATUS_POINTS = {
    "VERIFIED": 1.0,
    "UNVERIFIED": 0.6,
    "UNKNOWN": 0.3,
    "NOT CONFIRMED": 0.0,
}


def _verification_factor_score(fare: FareResult) -> Optional[float]:
    """Composite verification factor; None when no verification evidence exists."""
    verification = getattr(fare, "verification", None)
    if verification is None:
        return None
    statuses = {
        "fare": getattr(verification, "fare", "UNKNOWN"),
        "schedule": getattr(verification, "schedule", "UNKNOWN"),
        "seats": getattr(verification, "seats", "UNKNOWN"),
    }
    if all(status == "UNKNOWN" for status in statuses.values()):
        return None
    total_weight = sum(_VERIFICATION_FACTOR_WEIGHTS.values())
    return sum(
        _VERIFICATION_FACTOR_WEIGHTS[key] * _VERIFICATION_STATUS_POINTS.get(status, 0.3)
        for key, status in statuses.items()
    ) / total_weight


def _minmax_factor(values: dict[int, float], lower_is_better: bool = True) -> dict[int, float]:
    """Min-max normalize pool values into [0, 1]; identical values score 1.0."""
    if not values:
        return {}
    low, high = min(values.values()), max(values.values())
    if high == low:
        return {key: 1.0 for key in values}
    if lower_is_better:
        return {key: (high - value) / (high - low) for key, value in values.items()}
    return {key: (value - low) / (high - low) for key, value in values.items()}


def _airline_match_score(fare: FareResult, preference: str) -> float:
    requested = str(preference).strip().casefold()
    return float(
        requested in (fare.airline or "").casefold()
        or requested == (fare.airline_code or "").casefold()
    )


def _attach_score_breakdown(candidates: list[FareResult], request: SearchRequest) -> None:
    """Attach an explainable weighted score to every eligible candidate.

    Core factors default to price 35%, departure preference 20%, duration 15%,
    stops 10%, verification 20% (configurable via RANKING_*_WEIGHT). A factor
    whose data is missing for a candidate is excluded from that candidate's
    available weight. preferred_airline adds a conditional airline_preference
    factor only when the user asked for one.
    """
    price_values = {
        id(fare): price for fare in candidates if (price := _comparable_price(fare)) is not None
    }
    duration_values = {
        id(fare): minutes for fare in candidates if (minutes := duration_minutes(fare)) is not None
    }
    stop_values = {id(fare): float(fare.stops) for fare in candidates}
    price_scores = _minmax_factor(price_values)
    duration_scores = _minmax_factor(duration_values)
    stop_scores = _minmax_factor(stop_values)
    departure_preference = getattr(request, "preferred_departure", None)
    airline_preference = getattr(request, "preferred_airline", None)
    weights = {
        "price": config.ranking_price_weight,
        "departure": config.ranking_departure_weight,
        "duration": config.ranking_duration_weight,
        "stops": config.ranking_stops_weight,
        "verification": config.ranking_verification_weight,
        "airline_preference": config.ranking_airline_weight if airline_preference else 0.0,
    }

    for fare in candidates:
        factor_scores: dict[str, Optional[float]] = {
            "price": price_scores.get(id(fare)),
            "departure": _departure_score(fare, departure_preference),
            "duration": duration_scores.get(id(fare)),
            "stops": stop_scores.get(id(fare)),
            "verification": _verification_factor_score(fare),
            "airline_preference": (
                _airline_match_score(fare, airline_preference) if airline_preference else None
            ),
        }
        factors: dict[str, dict] = {}
        available_weight = 0.0
        weighted_total = 0.0
        missing_factors: list[str] = []
        for name, weight in weights.items():
            score = factor_scores.get(name)
            if weight <= 0:
                continue
            if score is None:
                missing_factors.append(name)
                continue
            weighted = weight * score
            available_weight += weight
            weighted_total += weighted
            factors[name] = {
                "weight": weight,
                "score": round(score, 4),
                "weighted": round(weighted, 4),
                "available": True,
            }
        overall = (weighted_total / available_weight * 100) if available_weight else 0.0
        fare.score_breakdown = {
            "score": round(overall, 2),
            "available_weight": round(available_weight, 4),
            "weights": {name: weights[name] for name in weights if weights[name] > 0},
            "factors": factors,
            "missing_factors": missing_factors,
        }


def _normalized_profile(value: Optional[str]) -> str:
    if not value:
        return "BEST_BALANCED"
    normalized = str(value).strip().upper()
    aliases = {
        "CHEAP": "CHEAPEST",
        "LOWEST_PRICE": "CHEAPEST",
        "FAST": "FASTEST",
        "QUICKEST": "FASTEST",
        "BALANCED": "BEST_BALANCED",
        "BESTBALANCED": "BEST_BALANCED",
        "LOWER_TRANSFER": "LOWER_TRANSFER_CONCERN",
        "TRANSFER_CONCERN": "LOWER_TRANSFER_CONCERN",
    }
    return aliases.get(normalized, normalized)


def compute_category_winners(
    candidates: list[FareResult], request: Optional[SearchRequest] = None
) -> dict[str, dict]:
    """Cheapest / fastest / best-overall winners from one eligible pool.

    No provider calls are made. Fastest requires a valid timezone-aware
    provider duration; otherwise it is labeled INSUFFICIENT DATA rather than
    ranked on guessed times.
    """
    winners: dict[str, dict] = {}
    priced = [
        (price, fare)
        for fare in candidates
        if (price := _comparable_price(fare)) is not None
    ]
    if priced:
        lowest = min(price for price, _ in priced)
        tied = [fare for price, fare in priced if price == lowest]
        first = tied[0]
        winners["cheapest"] = {
            "flight": first,
            "flight_number": first.flight_number,
            "label": "CHEAPEST",
            "metric": "comparable fare",
            "value": lowest,
            "currency": first.comparison_currency or first.currency,
            "tie": len(tied) > 1,
            "candidate_count": len(tied),
        }
    timed = [
        (minutes, fare)
        for fare in candidates
        if (minutes := duration_minutes(fare)) is not None
    ]
    if timed:
        shortest = min(minutes for minutes, _ in timed)
        tied = [fare for minutes, fare in timed if minutes == shortest]
        first = tied[0]
        winners["fastest"] = {
            "flight": first,
            "flight_number": first.flight_number,
            "label": "FASTEST",
            "metric": "provider-reported elapsed duration",
            "value": shortest,
            "unit": "minutes",
            "tie": len(tied) > 1,
            "candidate_count": len(tied),
        }
    else:
        winners["fastest"] = {
            "flight": None,
            "flight_number": None,
            "label": "INSUFFICIENT DATA",
            "metric": "provider-reported elapsed duration",
            "value": None,
            "unit": "minutes",
            "tie": False,
            "candidate_count": 0,
        }
    scored = [
        fare for fare in candidates if (fare.score_breakdown or {}).get("score") is not None
    ]
    if scored:
        best = max(
            scored, key=lambda fare: (fare.score_breakdown["score"], fare.flight_number)
        )
        winners["best_overall"] = {
            "flight": best,
            "flight_number": best.flight_number,
            "label": "BEST OVERALL",
            "metric": "5-factor weighted score",
            "value": best.score_breakdown["score"],
            "tie": False,
            "candidate_count": 1,
        }
    return winners


def rank_fares(candidates: list[FareResult], request: SearchRequest, profile: Optional[str] = None) -> list[FareResult]:
    """Sort already-eligible fares by a deterministic recommendation profile.

    Every candidate receives an explainable score_breakdown. The default
    BEST_BALANCED profile orders by that 5-factor score; explicit CHEAPEST /
    FASTEST / LOWER_TRANSFER_CONCERN profiles remain available as stable
    ranked alternatives without rerunning any provider requests.
    """
    ranked_input = list(candidates)
    _attach_score_breakdown(ranked_input, request)
    if len(ranked_input) < 2:
        return ranked_input

    selected_profile = _normalized_profile(profile or getattr(request, "recommendation_profile", None))

    def duration_key(fare: FareResult) -> tuple[float, str]:
        minutes = duration_minutes(fare)
        if minutes is None:
            return (float("inf"), fare.flight_number)
        return (float(minutes), fare.flight_number)

    def transfer_key(fare: FareResult) -> tuple[float, str]:
        return (_transfer_concern_score(fare), fare.flight_number)

    def profile_sort_key(fare: FareResult):
        if selected_profile == "CHEAPEST":
            return (
                _comparable_price(fare) is None,
                _price_or_inf(fare),
                0 if fare.price_status == "verified" else 1,
                fare.stops,
                fare.flight_number,
            )
        if selected_profile == "FASTEST":
            return (
                duration_key(fare)[0] == float("inf"),
                duration_key(fare)[0],
                _price_or_inf(fare),
                fare.flight_number,
            )
        if selected_profile == "LOWER_TRANSFER_CONCERN":
            return (
                transfer_key(fare)[0],
                duration_key(fare)[0],
                _price_or_inf(fare),
                fare.stops,
                fare.flight_number,
            )
        verification_factor = (fare.score_breakdown or {}).get("factors", {}).get("verification")
        verification_rank = -(verification_factor or {}).get("score", 0.0) if verification_factor else 0.0
        return (
            -(fare.score_breakdown or {}).get("score", 0.0),
            verification_rank,
            _price_or_inf(fare),
            fare.stops,
            fare.departure_time or fare.departure_at or "",
            fare.flight_number,
        )

    return sorted(ranked_input, key=profile_sort_key)
