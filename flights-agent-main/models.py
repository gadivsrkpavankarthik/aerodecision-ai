"""Domain models for T10 Flight Option Finder.

Strict Pydantic schemas representing flights, fares, search requests,
trace events, and verification results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from validation import (
    ValidationError,
    normalize_iata,
    validate_travel_date_not_past,
    validate_passengers,
    validate_budget,
    validate_currency,
    validate_cabin_class,
    validate_max_stops,
)


class FlightOption(BaseModel):
    """Normalized flight model representing an individual schedule/flight."""

    flight_number: str = Field(..., description="Normalized flight number e.g. 6E327")
    airline: Optional[str] = Field(None, description="Airline name e.g. IndiGo")
    airline_code: Optional[str] = Field(None, description="2-letter IATA airline code e.g. 6E")
    origin: str = Field(..., description="Origin airport IATA code e.g. DEL")
    destination: str = Field(..., description="Destination airport IATA code e.g. BOM")
    travel_date: str = Field(..., description="Travel date in YYYY-MM-DD format")
    departure_time: Optional[str] = Field(None, description="Local departure time e.g. 08:10")
    arrival_time: Optional[str] = Field(None, description="Local arrival time e.g. 10:20")
    departure_timezone: Optional[str] = Field(None, description="Departure airport timezone")
    arrival_timezone: Optional[str] = Field(None, description="Arrival airport timezone")
    retrieved_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when schedule data was retrieved",
    )
    stops: int = Field(0, description="Number of stops")
    source: str = Field("AviationStack", description="Source of flight schedule data")
    schedule_status: Literal["schedule_only", "unverified", "error"] = Field(
        "schedule_only",
        description="Schedule data is not proof of bookability",
    )

    @field_validator("origin", "destination")
    @classmethod
    def _iata(cls, v: str) -> str:
        return normalize_iata(v)

    @field_validator("travel_date")
    @classmethod
    def _travel_date(cls, v: str) -> str:
        return validate_travel_date_not_past(v, allow_past=True)


class ItineraryLeg(BaseModel):
    """Single segment within a multi-leg itinerary."""

    leg_index: int
    flight_number: str
    airline: Optional[str] = None
    origin: str
    destination: str
    travel_date: str
    departure_time: Optional[str] = None
    arrival_time: Optional[str] = None
    departure_at: Optional[str] = Field(None, description="ISO8601 tz-aware departure")
    arrival_at: Optional[str] = Field(None, description="ISO8601 tz-aware arrival")
    price: Optional[float] = None
    currency: Optional[str] = None
    price_status: Literal["verified", "unverified", "unavailable", "error"] = "unverified"
    retrieved_at: Optional[str] = None
    booking_url: Optional[str] = None
    booking_provider: Optional[str] = None
    booking_link_kind: Literal["provider_supplied", "provider_verified", "provider_search", "discovered_search", "none"] = "none"
    source: str = "Ignav"
    ignav_id: Optional[str] = None


class IndependentVerification(BaseModel):
    """Independent evidence statuses. Fare verification never proves a schedule."""

    fare: Literal["VERIFIED", "UNVERIFIED", "UNKNOWN", "NOT CONFIRMED"] = "UNKNOWN"
    schedule: Literal["VERIFIED", "UNVERIFIED", "UNKNOWN", "NOT CONFIRMED"] = "UNKNOWN"
    seats: Literal["VERIFIED", "UNVERIFIED", "UNKNOWN", "NOT CONFIRMED"] = "UNKNOWN"
    booking: Literal["VERIFIED", "UNVERIFIED", "UNKNOWN", "NOT CONFIRMED"] = "UNKNOWN"
    fare_source: Optional[str] = None
    schedule_source: Optional[str] = None
    booking_source: Optional[str] = None
    fare_retrieved_at: Optional[str] = None
    schedule_retrieved_at: Optional[str] = None
    verified_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    expires_at: Optional[str] = None
    evidence_age_seconds: Optional[float] = None
    freshness_status: Literal["FRESH", "AGING", "STALE", "UNKNOWN"] = "UNKNOWN"
    verification_source: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class FareResult(BaseModel):
    """Normalized fare model representing live pricing and match status."""

    flight_number: str = Field(..., description="Normalized flight number e.g. 6E327")
    association_level: Literal[
        "flight_specific", "itinerary_specific", "route_level", "airline_level", "unassociated"
    ] = Field("flight_specific", description="Provider association level for this fare record")
    comparison_amount: Optional[str] = None
    comparison_currency: Optional[str] = None
    fx_rate_source: Optional[str] = None
    fx_rate: Optional[str] = Field(None, description="Exchange rate applied for the comparison conversion")
    fx_observation_date: Optional[str] = None
    fx_retrieved_at: Optional[str] = None
    fx_status: Literal["same_currency", "converted", "unavailable", "stale", "unsupported"] = "same_currency"
    provider_fields: dict = Field(default_factory=dict, description="Sanitized raw provider fields retained for fare provenance")
    airline: Optional[str] = Field(None, description="Airline name")
    airline_code: Optional[str] = Field(None, description="Airline code e.g. 6E")
    origin: str = Field(..., description="Origin airport IATA code e.g. DEL")
    destination: str = Field(..., description="Destination airport IATA code e.g. BOM")
    travel_date: str = Field(..., description="Travel date in YYYY-MM-DD format")
    departure_time: Optional[str] = Field(None, description="Departure time")
    arrival_time: Optional[str] = Field(None, description="Arrival time")
    departure_at: Optional[str] = Field(None, description="Provider segment departure timestamp")
    arrival_at: Optional[str] = Field(None, description="Provider segment arrival timestamp")
    passengers: Optional[int] = None
    cabin_class: Optional[str] = None

    price: Optional[float] = Field(None, description="Authoritative fare amount")
    currency: Optional[str] = Field(None, description="Currency e.g. INR")
    price_available: bool = Field(False, description="Whether an exact fare is available")
    price_status: Literal["verified", "unverified", "unavailable", "error"] = Field(
        "unavailable", description="Fare verification status from provider"
    )
    availability_status: Literal["unknown", "provider_confirmed", "unavailable"] = Field(
        "unknown",
        description="Seat/booking availability; price verification alone does not confirm availability",
    )

    ignav_id: Optional[str] = Field(None, description="Unique Ignav itinerary identifier")

    booking_url: Optional[str] = Field(None, description="Direct booking link")
    booking_provider: Optional[str] = Field(None, description="Booking provider name")
    booking_provider_type: Optional[str] = Field(None, description="Provider type e.g. airline, ota")
    booking_link_kind: Literal["provider_supplied", "provider_verified", "provider_search", "discovered_search", "none"] = Field(
        "none", description="How the booking URL was obtained"
    )

    source: str = Field("Ignav", description="Authoritative fare source")
    retrieved_at: Optional[str] = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when fare was retrieved",
    )
    schedule_retrieved_at: Optional[str] = Field(
        None, description="Timestamp when the matched schedule was retrieved"
    )

    web_verification_status: Literal["passed", "partial", "unavailable", "failed"] = Field(
        "unavailable", description="Auxiliary booking-page verification status"
    )
    web_verification_url: Optional[str] = Field(None, description="URL checked for page verification")

    fare_source: Optional[str] = Field(None, description="Source of the fare quote")
    fare_status: Literal[
        "Confirmed by provider",
        "Matched",
        "Fare only",
        "Schedule only",
        "Not matched",
        "Unavailable",
        "Stale",
        "Unknown",
        "Not independently verified",
    ] = Field("Unknown", description="Normalized fare evidence status")
    schedule_source: Optional[str] = Field(None, description="Source of schedule data")
    schedule_status: Literal[
        "Confirmed by provider",
        "Matched",
        "Fare only",
        "Schedule only",
        "Not matched",
        "Unavailable",
        "Stale",
        "Unknown",
        "Not independently verified",
    ] = Field("Unknown", description="Normalized schedule evidence status")
    evidence_messages: list[str] = Field(default_factory=list, description="Plain-language evidence notes")
    overall_data_freshness: Literal["Fresh", "Aging", "Stale", "Unknown"] = Field(
        "Unknown", description="Freshness label for combined evidence"
    )
    evidence: Optional[FlightEvidence] = Field(None, description="Structured evidence object")
    trust_status: dict = Field(
        default_factory=dict,
        description="Centralized user-facing fare/schedule/availability status mapping",
    )
    verification: IndependentVerification = Field(
        default_factory=IndependentVerification,
        description="Independent fare, schedule, seat, and booking statuses",
    )
    score_breakdown: dict = Field(default_factory=dict, description="Explainable ranking factor scores")

    stops: int = Field(0, description="Number of stops")
    itinerary_kind: Literal["direct", "connecting_self_transfer"] = Field(
        "direct", description="Direct ticket vs separately booked connection"
    )
    connection_hub: Optional[str] = Field(None, description="Transit hub IATA for connections")
    layover_minutes: Optional[int] = Field(None, description="Layover duration in minutes")
    legs: Optional[list[ItineraryLeg]] = Field(None, description="Per-leg detail for connections")
    combined_price_note: Optional[str] = Field(
        None, description="Explanation when combined price is omitted or approximate"
    )
    through_ticket_confirmed: bool = Field(
        False, description="True only when provider explicitly confirms a through ticket"
    )
    schedule_matched: bool = Field(
        False, description="True when this fare was matched to an AviationStack schedule"
    )
    match_confidence: Optional[Literal["EXACT_MATCH", "HIGH_CONFIDENCE_MATCH", "PARTIAL_MATCH", "NO_MATCH"]] = Field(
        None, description="Deterministic schedule-fare match classification"
    )
    match_fields: list[str] = Field(default_factory=list, description="Fields that contributed to the match")
    booking_state: Optional[Literal[
        "LINK_AVAILABLE", "LINK_INVALID", "LINK_UNREACHABLE", "BOOKING_DETAILS_UNCONFIRMED",
        "FARE_RECHECK_REQUIRED", "BOOKABILITY_UNCONFIRMED", "NO_LINK"
    ]] = Field(None, description="Booking-link state; never implies completed bookability")
    constraint_status: Optional[str] = Field(
        None,
        description="eligible, over_budget, foreign_carrier, cabin_mismatch, missing_price, route_mismatch, date_mismatch",
    )
    constraint_reason: Optional[str] = Field(None, description="Why this fare was kept or excluded")

    @field_validator("origin", "destination")
    @classmethod
    def _iata(cls, v: str) -> str:
        return normalize_iata(v)


class FlightEvidence(BaseModel):
    """Normalized evidence for a fare/schedule recommendation.

    The evidence model keeps provider results, matching state, booking-page checks,
    and availability separately so the UI can explain what is verified and what is still
    unconfirmed without collapsing everything into a single generic status.
    """

    fare_source: Optional[str] = Field(None, description="Source of the price quote")
    fare_retrieved_at: Optional[str] = Field(None, description="When the fare quote was retrieved")
    fare_status: Literal[
        "Confirmed by provider",
        "Matched",
        "Fare only",
        "Schedule only",
        "Not matched",
        "Unavailable",
        "Stale",
        "Unknown",
        "Not independently verified",
    ] = Field("Unknown", description="Provider-originated fare status")
    schedule_source: Optional[str] = Field(None, description="Source of schedule data")
    schedule_retrieved_at: Optional[str] = Field(None, description="When the schedule data was retrieved")
    schedule_status: Literal[
        "Confirmed by provider",
        "Matched",
        "Fare only",
        "Schedule only",
        "Not matched",
        "Unavailable",
        "Stale",
        "Unknown",
        "Not independently verified",
    ] = Field("Unknown", description="Schedule evidence status")
    schedule_matched: Optional[bool] = Field(None, description="Whether fare and schedule were matched")
    booking_url: Optional[str] = Field(None, description="Direct booking page URL, when known")
    booking_verification_status: Literal["passed", "partial", "unavailable", "failed", "unknown"] = Field(
        "unknown", description="Verification evidence from the booking page"
    )
    booking_checked_at: Optional[str] = Field(None, description="When booking-page verification was performed")
    availability_status: Literal["unknown", "provider_confirmed", "unavailable"] = Field(
        "unknown", description="Seat or booking availability status"
    )
    evidence_messages: list[str] = Field(default_factory=list, description="Plain-language explanations")
    overall_data_freshness: Literal["Fresh", "Aging", "Stale", "Unknown"] = Field(
        "Unknown", description="Combined freshness label for all evidence"
    )


class SearchRequest(BaseModel):
    """Structured user flight search request."""

    origin_city: Optional[str] = Field(None, description="Origin city name")
    destination_city: Optional[str] = Field(None, description="Destination city name")
    origin_code: Optional[str] = Field(None, description="Origin IATA code")
    destination_code: Optional[str] = Field(None, description="Destination IATA code")
    travel_date: Optional[str] = Field(
        None, description="Date in YYYY-MM-DD format; None when the user has not resolved one"
    )
    budget: Optional[float] = Field(None, description="Maximum budget amount")
    currency: str = Field("INR", description="Currency code")
    passengers: int = Field(1, description="Number of passengers")
    cabin_class: str = Field("economy", description="Cabin class: economy, premium_economy, business, first")
    recommendation_profile: Literal["CHEAPEST", "FASTEST", "BEST_BALANCED", "LOWER_TRANSFER_CONCERN"] = Field(
        "BEST_BALANCED", description="User-selected recommendation profile"
    )
    preferred_airline: Optional[str] = Field(None, description="Preferred airline name or code")
    preferred_departure: Optional[Literal["morning", "afternoon", "evening", "night"]] = Field(
        None, description="Preferred time of day: morning, afternoon, evening, night"
    )
    max_stops: Optional[int] = Field(None, description="Maximum allowed stops")

    @field_validator("travel_date")
    @classmethod
    def _travel_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return validate_travel_date_not_past(v, allow_past=False)

    @field_validator("passengers")
    @classmethod
    def _passengers(cls, v: int) -> int:
        return validate_passengers(v)

    @field_validator("budget")
    @classmethod
    def _budget(cls, v: Optional[float]) -> Optional[float]:
        return validate_budget(v)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return validate_currency(v)

    @field_validator("cabin_class")
    @classmethod
    def _cabin(cls, v: str) -> str:
        return validate_cabin_class(v)

    @field_validator("max_stops")
    @classmethod
    def _max_stops(cls, v: Optional[int]) -> Optional[int]:
        return validate_max_stops(v)

    @field_validator("origin_code", "destination_code")
    @classmethod
    def _optional_iata(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        return normalize_iata(v)

    @field_validator("origin_city", "destination_city")
    @classmethod
    def _normalize_city(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def _origin_dest_distinct(self) -> "SearchRequest":
        if self.origin_code and self.destination_code and self.origin_code == self.destination_code:
            raise ValidationError("Origin and destination must differ.", field="destination_code")
        return self


class BookingOption(BaseModel):
    """Booking option returned by Ignav booking links endpoint."""

    provider: str = Field(..., description="Provider name")
    provider_type: Optional[str] = Field(None, description="airline or ota")
    price: Optional[float] = Field(None, description="Provider price")
    currency: Optional[str] = Field(None, description="Provider currency")
    url: str = Field(..., description="Booking URL")


class ScrapingVerificationResult(BaseModel):
    """Result of auxiliary booking page verification (requests + BeautifulSoup).

    Status meanings:
    - passed: Page text shows expected route/flight AND fare with reliable evidence (not URL alone).
    - partial: Route or flight visible; fare not reliably confirmed on page.
    - unavailable: Blocked, timeout, dynamic render, or insufficient evidence.
    - failed: Page content conflicts with expected itinerary.
    """

    status: Literal["passed", "partial", "unavailable", "failed"] = Field(
        ..., description="Verification outcome"
    )
    url: Optional[str] = Field(None, description="Final URL inspected")
    original_url: Optional[str] = Field(None, description="URL before redirects")
    extracted_provider: Optional[str] = Field(None, description="Provider extracted from page")
    extracted_flight_number: Optional[str] = Field(None, description="Flight number extracted")
    extracted_route: Optional[str] = Field(None, description="Route extracted")
    extracted_date: Optional[str] = Field(None, description="Date extracted")
    extracted_fare: Optional[float] = Field(None, description="Price extracted from page")
    details: Optional[str] = Field(None, description="Additional verification notes")


class AgentTraceStep(BaseModel):
    """Step in the agent's PLAN -> ACT -> OBSERVE -> EVALUATE -> DECIDE execution trace."""

    phase: Literal["PLAN", "ACT", "OBSERVE", "EVALUATE", "DECIDE"] = Field(
        ..., description="Trace phase"
    )
    content: str = Field(..., description="Explanation / reasoning / plan")
    tool_calls: Optional[list[dict]] = Field(None, description="Tools called during ACT phase")
    observations: Optional[str] = Field(None, description="Observation summary")


class FlightSearchResult(BaseModel):
    """Complete flight search result and execution artifact."""

    query: str = Field(..., description="Original user prompt")
    search_request: Optional[SearchRequest] = Field(None, description="Extracted search parameters")
    flights_found: list[FlightOption] = Field(default_factory=list, description="AviationStack flight schedules")
    fares_found: list[FareResult] = Field(default_factory=list, description="Ignav fares retrieved")
    unassociated_fare_records: list[dict] = Field(
        default_factory=list,
        description="Provider fare indications that cannot safely be attached to a flight",
    )
    matched_fares: list[FareResult] = Field(default_factory=list, description="Exact-matched flight fares")
    valid_candidates: list[FareResult] = Field(default_factory=list, description="Candidates within budget & constraints")
    best_flight: Optional[FareResult] = Field(None, description="Selected winning flight recommendation")
    booking_option: Optional[BookingOption] = Field(None, description="Selected booking option")
    web_verification: Optional[ScrapingVerificationResult] = Field(None, description="Booking page verification result")
    trace: list[AgentTraceStep] = Field(default_factory=list, description="Transparent agent trace")
    recommendation_reason: Optional[str] = Field(None, description="Explanation for selection")
    recommendations: list[FareResult] = Field(default_factory=list, description="Alternative recommendation set for the active profile")
    recommendation_profile: Literal["CHEAPEST", "FASTEST", "BEST_BALANCED", "LOWER_TRANSFER_CONCERN"] = Field(
        "BEST_BALANCED", description="Recommendation profile used when selecting a best flight"
    )
    suggestions: list[str] = Field(default_factory=list, description="Smart recommendations when no exact flight is found")
    formatted_response: str = Field(..., description="Final user-facing formatted response")
    request_id: Optional[str] = Field(None, description="Correlation ID for logs and API responses")
    searched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp when the search completed",
    )
    provider_statuses: dict[str, str] = Field(default_factory=dict)
    provider_durations_ms: dict[str, int] = Field(default_factory=dict)
    error_code: Optional[str] = Field(None, description="Machine-readable error when search aborted early")
    decision_workspace: dict = Field(
        default_factory=dict,
        description="Computed evidence, cost, risk, alternatives, recommendations, and readiness",
    )
    execution_status: str = Field(
        "NO RESULTS",
        description="Backend-derived overall provider/search execution state",
    )
