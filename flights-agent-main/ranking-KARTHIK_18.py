"""Normalized, preference-only ranking for eligible flight fares."""

from __future__ import annotations

import re
from typing import Optional

from config import config
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
    if not preference:
        return None
    hour = _parse_departure_hour(fare.departure_time)
    if hour is None:
        return 0.0
    matches = {
        "morning": 5 <= hour < 12,
        "afternoon": 12 <= hour < 17,
        "evening": 17 <= hour < 21,
        "night": hour >= 21 or hour < 5,
    }
    normalized_preference = str(preference).strip().casefold()
    return float(matches.get(normalized_preference, False))


def _parse_duration_minutes(fare: FareResult) -> Optional[int]:
    """Estimate journey duration using provider timestamps when available."""
    departure = fare.departure_at or (
        f"{fare.travel_date}T{fare.departure_time}:00Z" if fare.departure_time else None
    )
    arrival = fare.arrival_at or (
        f"{fare.travel_date}T{fare.arrival_time}:00Z" if fare.arrival_time else None
    )
    if not departure or not arrival:
        return None

    try:
        dep = __import__("datetime").datetime.fromisoformat(departure.replace("Z", "+00:00"))
        arr = __import__("datetime").datetime.fromisoformat(arrival.replace("Z", "+00:00"))
    except ValueError:
        return None

    if dep.tzinfo is None:
        dep = dep.replace(tzinfo=__import__("datetime").timezone.utc)
    if arr.tzinfo is None:
        arr = arr.replace(tzinfo=__import__("datetime").timezone.utc)
    delta = arr - dep
    return int(delta.total_seconds() // 60) if delta.total_seconds() >= 0 else None


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


def rank_fares(candidates: list[FareResult], request: SearchRequest, profile: Optional[str] = None) -> list[FareResult]:
    """Sort already-eligible fares by a deterministic recommendation profile.

    The legacy behavior is preserved as the default BEST_BALANCED profile, while
    explicit CHEAPEST / FASTEST / LOWER_TRANSFER_CONCERN profiles provide a stable
    ranked alternative without rerunning any provider requests.
    """
    if len(candidates) < 2:
        return list(candidates)

    selected_profile = _normalized_profile(profile or getattr(request, "recommendation_profile", None))
    def comparison_price(fare: FareResult):
        if fare.comparison_amount is not None:
            try:
                return float(fare.comparison_amount)
            except (TypeError, ValueError):
                return None
        return float(fare.price) if fare.price is not None else None

    prices = [price for fare in candidates if (price := comparison_price(fare)) is not None]
    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None
    weights = {
        "price": config.ranking_price_weight,
        "airline": config.ranking_airline_weight,
        "departure": config.ranking_departure_weight,
    }

    def preference_score(fare: FareResult) -> float:
        components: list[tuple[float, float]] = []
        fare_price = comparison_price(fare)
        if weights["price"] > 0 and min_price is not None and max_price is not None and fare_price is not None:
            if max_price == min_price:
                price_score = 1.0
            else:
                price_score = (max_price - fare_price) / (max_price - min_price)
            components.append((weights["price"], price_score))
        if getattr(request, "preferred_airline", None) and weights["airline"] > 0:
            requested = str(request.preferred_airline).strip().casefold()
            airline_match = requested in (fare.airline or "").casefold()
            airline_match = airline_match or requested == (fare.airline_code or "").casefold()
            components.append((weights["airline"], float(airline_match)))
        departure_score = _departure_score(fare, getattr(request, "preferred_departure", None))
        if departure_score is not None and weights["departure"] > 0:
            components.append((weights["departure"], departure_score))
        total_weight = sum(weight for weight, _ in components)
        if not total_weight:
            return 0.0
        return sum(weight * score for weight, score in components) / total_weight

    def duration_key(fare: FareResult) -> tuple[float, str]:
        duration = _parse_duration_minutes(fare)
        if duration is None:
            return (float("inf"), fare.flight_number)
        return (float(duration), fare.flight_number)

    def transfer_key(fare: FareResult) -> tuple[float, str]:
        return (_transfer_concern_score(fare), fare.flight_number)

    def profile_sort_key(fare: FareResult):
        if selected_profile == "CHEAPEST":
            return (
                comparison_price(fare) is None,
                comparison_price(fare) if comparison_price(fare) is not None else float("inf"),
                0 if fare.price_status == "verified" else 1,
                fare.stops,
                fare.flight_number,
            )
        if selected_profile == "FASTEST":
            return (
                duration_key(fare)[0] == float("inf"),
                duration_key(fare)[0],
                comparison_price(fare) if comparison_price(fare) is not None else float("inf"),
                fare.flight_number,
            )
        if selected_profile == "LOWER_TRANSFER_CONCERN":
            return (
                transfer_key(fare)[0],
                duration_key(fare)[0],
                comparison_price(fare) if comparison_price(fare) is not None else float("inf"),
                fare.stops,
                fare.flight_number,
            )
        return (
            -preference_score(fare),
            duration_key(fare)[0] == float("inf"),
            duration_key(fare)[0],
            0 if fare.price_status == "verified" else 1,
            fare.stops,
            comparison_price(fare) if comparison_price(fare) is not None else float("inf"),
            fare.flight_number,
        )

    return sorted(candidates, key=profile_sort_key)
