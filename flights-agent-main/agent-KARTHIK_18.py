"""Agent Orchestration module for T10 Flight Option Finder.

Implements the complete PLAN -> ACT -> OBSERVE -> EVALUATE -> DECIDE loop,
integrates SessionMemory, strictly enforces hard constraints, selects booking links,
runs auxiliary requests + BeautifulSoup verification, and formats transparent user-facing output.
"""

import json
import math
import re
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Any, Callable
from datetime import datetime, timedelta, timezone, date
from time import perf_counter
from pydantic import ValidationError as PydanticValidationError

from config import config
from models import (
    SearchRequest,
    FlightOption,
    FareResult,
    BookingOption,
    ScrapingVerificationResult,
    AgentTraceStep,
    FlightSearchResult,
)
from memory import SessionMemory
from validation import ValidationError
from connection_utils import (
    validate_hub_connection,
    build_itinerary_legs,
    combined_price_if_compatible,
    oldest_timestamp,
)
from airport_resolver import resolve_airport
from matching import match_flights_with_fares
from ranking import rank_fares
from tools import search_flights, AGENT_TOOLS_SCHEMA
from llm_client import nim_client, NIMError
from ignav_client import ignav_client
from services.fx_service import convert as convert_fx, FXUnavailable
from scraping.booking_verifier import booking_verifier

logger = logging.getLogger(__name__)


def _default_travel_date() -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=14)).isoformat()


def _default_year() -> str:
    return str(datetime.now(timezone.utc).year)


def _currency_symbol(currency: str) -> str:
    return {
        "INR": "₹",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "AED": "AED ",
        "SGD": "S$",
        "AUD": "A$",
        "CAD": "C$",
    }.get(currency.upper(), f"{currency.upper()} ")


def _freshness_label(timestamp: Optional[str], fresh_minutes: int = 15) -> str:
    if not timestamp:
        return "UNKNOWN"
    try:
        retrieved = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "UNKNOWN"
    age_minutes = max(0, (datetime.now(timezone.utc) - retrieved.astimezone(timezone.utc)).total_seconds() / 60)
    if age_minutes <= fresh_minutes:
        return "FRESH"
    if age_minutes <= fresh_minutes * 4:
        return "AGING"
    return "STALE"


def _timed_provider_call(
    callback: Callable[..., Any], **kwargs: Any
) -> tuple[Optional[Any], str, int, Optional[str]]:
    started = perf_counter()
    try:
        value = callback(**kwargs)
        status = value.get("status", "success") if isinstance(value, dict) else "success"
        return value, str(status), round((perf_counter() - started) * 1000), None
    except Exception as exc:
        logger.warning(
            "Flight provider call failed (%s): %s",
            getattr(callback, "__name__", "provider"),
            type(exc).__name__,
        )
        return None, "error", round((perf_counter() - started) * 1000), type(exc).__name__


def _is_indian_domestic_carrier(fare: FareResult) -> bool:
    flight_code = re.match(r"^([A-Z0-9]{2})", (fare.flight_number or "").upper())
    carrier_code = (fare.airline_code or (flight_code.group(1) if flight_code else "")).upper()
    if carrier_code:
        return carrier_code in INDIAN_DOMESTIC_CARRIER_CODES
    airline = (fare.airline or "").strip().lower()
    return any(name in airline for name in INDIAN_DOMESTIC_CARRIER_NAMES)


INDIAN_DOMESTIC_CARRIER_NAMES = (
    "indigo",
    "air india",
    "air india express",
    "spicejet",
    "akasa",
    "alliance air",
    "star air",
    "vistara",
)
INDIAN_DOMESTIC_CARRIER_CODES = {"6E", "AI", "SG", "QP", "9I", "S5", "IX", "I5", "UK"}

NEARBY_HUBS: dict[str, list[str]] = {
    "VGA": ["HYD (Hyderabad)", "MAA (Chennai)", "VTZ (Visakhapatnam)"],
    "PNQ": ["BOM (Mumbai)"],
    "BOM": ["PNQ (Pune)"],
    "DEL": ["IXC (Chandigarh)", "JAI (Jaipur)"],
    "BLR": ["MAA (Chennai)", "COK (Kochi)"],
    "GOI": ["GOX (Goa Mopa)", "BOM (Mumbai)", "BLR (Bengaluru)"],
    "GOX": ["GOI (Goa Dabolim)", "BOM (Mumbai)", "BLR (Bengaluru)"],
    "TRV": ["COK (Kochi)"],
    "IXC": ["DEL (Delhi)"],
    "DED": ["DEL (Delhi)"],
    "JAI": ["DEL (Delhi)"],
    "LKO": ["DEL (Delhi)", "VNS (Varanasi)"],
    "PAT": ["CCU (Kolkata)", "VNS (Varanasi)"],
}


class FlightOptionFinderAgent:
    """Production AI Agent for finding and verifying flight options."""

    def __init__(self, memory: Optional[SessionMemory] = None):
        self.memory = memory or SessionMemory()

    def parse_user_query(self, query: str) -> SearchRequest:
        """Parse natural language query into a structured SearchRequest, merging with session memory."""
        prefs = self.memory.get_preferences()

        # Extract dates: YYYY-MM-DD or Month DD, YYYY or Month DD
        date_val: Optional[str] = prefs.travel_date
        
        # Regex for YYYY-MM-DD
        iso_date = re.search(r"\b(20\d\d-\d{2}-\d{2})\b", query)
        if iso_date:
            date_val = iso_date.group(1)
        else:
            # Match formats like 'September 24 2026', '24 September 2026', 'September 24'
            month_match = re.search(
                r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(20\d\d))?\b",
                query,
                re.IGNORECASE,
            )
            if not month_match:
                # Match '24 September 2026'
                month_match = re.search(
                    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)(?:\s*,?\s*(20\d\d))?\b",
                    query,
                    re.IGNORECASE,
                )
                if month_match:
                    day = month_match.group(1)
                    month_name = month_match.group(2)
                    year = month_match.group(3) or _default_year()
                    try:
                        dt = datetime.strptime(f"{year} {month_name[:3]} {day}", "%Y %b %d")
                        date_val = dt.strftime("%Y-%m-%d")
                    except ValueError:
                        pass
            else:
                month_name = month_match.group(1)
                day = month_match.group(2)
                year = month_match.group(3) or _default_year()
                try:
                    dt = datetime.strptime(f"{year} {month_name[:3]} {day}", "%Y %b %d")
                    date_val = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

        # Extract budget: under ₹8,000, under 8000, max 8000, budget 8000
        budget_val: Optional[float] = prefs.budget
        # Search specifically for budget keyword context
        budget_specific = re.search(
            r"(?:under|below|budget|max|within|less than)\s*"
            r"(?:[₹$€£]\s*)?(?:(?:INR|USD|EUR|GBP|AED|SGD|AUD|CAD)\s*)?"
            r"([0-9][0-9,]*(?:\.\d{1,2})?)",
            query,
            re.IGNORECASE,
        )
        if budget_specific:
            num_str = budget_specific.group(1).replace(",", "")
            try:
                budget_val = float(num_str)
            except ValueError:
                pass

        currency_val = prefs.currency or "INR"
        currency_code = re.search(r"\b(INR|USD|EUR|GBP|AED|SGD|AUD|CAD)\b", query, re.IGNORECASE)
        if currency_code:
            currency_val = currency_code.group(1).upper()
        else:
            for symbol, code in (("₹", "INR"), ("$", "USD"), ("€", "EUR"), ("£", "GBP")):
                if symbol in query:
                    currency_val = code
                    break

        # Extract cities / routes
        orig_city = prefs.origin_city
        dest_city = prefs.destination_city

        # 1. Try 'from X to Y'
        m1 = re.search(r"\bfrom\s+([A-Za-z0-9\s\(\)\-\.]+?)\s+to\s+([A-Za-z0-9\s\(\)\-\.]+?)(?:\s+(?:on|under|below|for|with|in|at|budget|before|after)\b|$)", query, re.IGNORECASE)
        if m1:
            c1, c2 = m1.group(1).strip(), m1.group(2).strip()
            r1, r2 = resolve_airport(c1), resolve_airport(c2)
            orig_city = r1.city if r1 else c1
            dest_city = r2.city if r2 else c2

        if not orig_city or not dest_city:
            # 2. Try 'X to Y' or 'X -> Y'
            m2 = re.search(r"\b([A-Za-z0-9\s\(\)\-\.]+?)\s+(?:to|->|-)\s+([A-Za-z0-9\s\(\)\-\.]+?)(?:\s+(?:on|under|below|for|with|in|at|budget)\b|$)", query, re.IGNORECASE)
            if m2:
                c1 = re.sub(r"^(find|get|search|show|book|a|me|flights?)\s+", "", m2.group(1).strip(), flags=re.IGNORECASE).strip()
                c2 = m2.group(2).strip()
                r1, r2 = resolve_airport(c1), resolve_airport(c2)
                orig_city = r1.city if r1 else c1
                dest_city = r2.city if r2 else c2

        # Preferred departure time
        pref_dep = prefs.preferred_departure
        if re.search(r"\b(morning|early morning)\b", query, re.IGNORECASE):
            pref_dep = "morning"
        elif re.search(r"\b(afternoon)\b", query, re.IGNORECASE):
            pref_dep = "afternoon"
        elif re.search(r"\b(evening)\b", query, re.IGNORECASE):
            pref_dep = "evening"
        elif re.search(r"\b(night|late night)\b", query, re.IGNORECASE):
            pref_dep = "night"

        # Preferred airline
        pref_airline = prefs.preferred_airline
        for air in ["IndiGo", "Air India", "Vistara", "SpiceJet", "Akasa", "Emirates"]:
            if air.lower() in query.lower():
                pref_airline = air
                break

        # Cabin class (accept both "premium economy" and form value "premium_economy")
        cabin = prefs.cabin_class or "economy"
        query_cabin = re.sub(r"[_-]+", " ", query.lower())
        if re.search(r"\bbusiness\b", query_cabin):
            cabin = "business"
        elif "premium economy" in query_cabin:
            cabin = "premium_economy"
        elif "first class" in query_cabin:
            cabin = "first"

        # Passengers
        pax = prefs.passengers or 1
        pax_match = re.search(r"(\d+)\s*(?:passengers?|pax|adults?|persons?|people)", query, re.IGNORECASE)
        if pax_match:
            try:
                pax = int(pax_match.group(1))
            except ValueError:
                pass

        max_stops = prefs.max_stops
        if re.search(r"\b(nonstop|non-stop|direct)\b", query, re.IGNORECASE):
            max_stops = 0
        else:
            stops_match = re.search(
                r"\b(?:max(?:imum)?|up to|no more than)\s*(\d+)\s*stops?\b",
                query,
                re.IGNORECASE,
            )
            if not stops_match:
                stops_match = re.search(r"\b(\d+)\s*stops?\b", query, re.IGNORECASE)
            if stops_match:
                max_stops = int(stops_match.group(1))

        # Update memory
        self.memory.update(
            budget=budget_val,
            currency=currency_val,
            origin_city=orig_city,
            destination_city=dest_city,
            travel_date=date_val,
            preferred_departure=pref_dep,
            preferred_airline=pref_airline,
            cabin_class=cabin,
            passengers=pax,
            max_stops=max_stops,
        )

        request = SearchRequest(
            origin_city=orig_city,
            destination_city=dest_city,
            travel_date=date_val or _default_travel_date(),
            budget=budget_val,
            currency=currency_val,
            passengers=pax,
            cabin_class=cabin,
            preferred_airline=pref_airline,
            preferred_departure=pref_dep,
            max_stops=max_stops,
        )
        return self._interpret_request(query, request, prefs)

    def _interpret_request(self, query: str, request: SearchRequest, prefs) -> SearchRequest:
        """Use NIM for structured extraction while preserving deterministic explicit values."""
        if not config.nvidia_api_key:
            return request
        try:
            message = nim_client.chat_completion(
                [
                    {
                        "role": "system",
                        "content": (
                            f"Today is {datetime.now(timezone.utc).date().isoformat()}. Extract flight-search "
                            "fields from the user request. Return only a JSON object "
                            "using SearchRequest field names: origin_city, destination_city, travel_date, "
                            "budget, currency, passengers, cabin_class, preferred_airline, preferred_departure, "
                            "max_stops. Resolve relative dates from today's date. Use null for fields that are "
                            "not stated. Do not invent missing values."
                        ),
                    },
                    {"role": "user", "content": query},
                ],
                temperature=0,
                max_tokens=300,
            )
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("NVIDIA NIM returned no structured request content.")
            clean_content = content.strip()
            if clean_content.startswith("```"):
                clean_content = clean_content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            structured = json.loads(clean_content)
            if not isinstance(structured, dict):
                raise ValueError("NVIDIA NIM request interpretation must be a JSON object.")
            allowed_fields = set(SearchRequest.model_fields)
            if set(structured) - allowed_fields:
                raise ValueError("NVIDIA NIM returned unknown SearchRequest fields.")

            values = request.model_dump()
            for field in ("origin_city", "destination_city"):
                if (
                    (not values[field] or not resolve_airport(values[field]))
                    and structured.get(field)
                ):
                    values[field] = structured[field]
            explicit_values = {
                "travel_date": bool(re.search(r"\b20\d\d-\d{2}-\d{2}\b|"
                    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
                    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b|\b\d{1,2}\s+"
                    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
                    query, re.IGNORECASE)) or prefs.travel_date is not None,
                "budget": bool(re.search(r"\b(?:under|below|budget|max|within|less than)\b", query, re.IGNORECASE))
                    or prefs.budget is not None,
                "currency": bool(re.search(r"\b(?:INR|USD|EUR|GBP|AED|SGD|AUD|CAD)\b|[₹$€£]", query, re.IGNORECASE)),
                "passengers": bool(re.search(r"\d+\s*(?:passengers?|pax|adults?|persons?|people)", query, re.IGNORECASE))
                    or prefs.passengers > 1,
                "cabin_class": bool(re.search(r"\b(?:economy|business|first|premium)\b", query, re.IGNORECASE))
                    or prefs.cabin_class != "economy",
                "preferred_airline": any(
                    airline.lower() in query.lower()
                    for airline in ("IndiGo", "Air India", "Vistara", "SpiceJet", "Akasa", "Emirates")
                ) or prefs.preferred_airline is not None,
                "preferred_departure": bool(re.search(r"\b(?:morning|afternoon|evening|night)\b", query, re.IGNORECASE))
                    or prefs.preferred_departure is not None,
                "max_stops": bool(re.search(r"\b(?:nonstop|non-stop|direct|\d+\s*stops?)\b", query, re.IGNORECASE))
                    or prefs.max_stops is not None,
            }
            for field, is_explicit in explicit_values.items():
                if not is_explicit and structured.get(field) is not None:
                    values[field] = structured[field]
            interpreted = SearchRequest.model_validate(values)
            self.memory.update(
                origin_city=interpreted.origin_city,
                destination_city=interpreted.destination_city,
                travel_date=interpreted.travel_date,
                budget=interpreted.budget,
                currency=interpreted.currency,
                passengers=interpreted.passengers,
                cabin_class=interpreted.cabin_class,
                preferred_airline=interpreted.preferred_airline,
                preferred_departure=interpreted.preferred_departure,
                max_stops=interpreted.max_stops,
            )
            return interpreted
        except (NIMError, ValueError, TypeError) as exc:
            logger.warning("NVIDIA NIM request interpretation unavailable; using deterministic parsing: %s", exc)
            return request

    def run(
        self,
        user_query: str,
        request_id: Optional[str] = None,
        overrides: Optional[dict[str, Any]] = None,
    ) -> FlightSearchResult:
        """Execute the complete PLAN -> ACT -> OBSERVE -> EVALUATE -> DECIDE agent workflow."""
        trace: list[AgentTraceStep] = []
        req_id = request_id or str(uuid.uuid4())

        # 1. PLAN Phase: Parse request and establish goal
        try:
            search_req = self.parse_user_query(user_query)
            if overrides:
                payload = search_req.model_dump()
                for key, value in overrides.items():
                    if key in payload:
                        payload[key] = value
                search_req = SearchRequest(**payload)
                for key in overrides:
                    if key in SearchRequest.model_fields:
                        setattr(self.memory.preferences, key, getattr(search_req, key))
        except (ValidationError, PydanticValidationError) as e:
            msg = f"Invalid search parameters: {e}"
            return FlightSearchResult(
                query=user_query,
                trace=[AgentTraceStep(phase="PLAN", content=msg)],
                formatted_response=msg,
                request_id=req_id,
                error_code="validation_error",
            )

        orig_res = resolve_airport(search_req.origin_city or "")
        dest_res = resolve_airport(search_req.destination_city or "")

        plan_desc = (
            f"Plan flight search for route {search_req.origin_city} ({orig_res.code if orig_res else '?'}) -> "
            f"{search_req.destination_city} ({dest_res.code if dest_res else '?'}) on date {search_req.travel_date}. "
            f"Budget constraint: {_currency_symbol(search_req.currency)}{search_req.budget:,.0f}." if search_req.budget else "No budget constraint specified."
        )
        trace.append(AgentTraceStep(phase="PLAN", content=plan_desc))

        if not orig_res or not dest_res:
            err_resp = f"Could not determine valid airport codes for {search_req.origin_city} -> {search_req.destination_city}. Please clarify the cities."
            return FlightSearchResult(
                query=user_query,
                search_request=search_req,
                trace=trace,
                formatted_response=err_resp,
                request_id=req_id,
            )

        if orig_res.code == dest_res.code:
            msg = "Origin and destination must resolve to different airports."
            return FlightSearchResult(
                query=user_query,
                search_request=search_req,
                trace=trace + [AgentTraceStep(phase="PLAN", content=msg)],
                formatted_response=msg,
                request_id=req_id,
                error_code="validation_error",
            )

        search_req.origin_code = orig_res.code
        search_req.destination_code = dest_res.code

        # 2. ACT Phase 1: Tool 1 - search_flights()
        act1_desc = f"Calling search_flights(origin='{orig_res.code}', destination='{dest_res.code}', travel_date='{search_req.travel_date}')"
        trace.append(AgentTraceStep(
            phase="ACT",
            content=act1_desc,
            tool_calls=[{
                "tool": "search_flights",
                "args": {
                    "origin": orig_res.code,
                    "destination": dest_res.code,
                    "travel_date": search_req.travel_date,
                    "passengers": search_req.passengers,
                    "cabin_class": search_req.cabin_class,
                }
            }]
        ))

        with ThreadPoolExecutor(max_workers=2) as executor:
            schedule_future = executor.submit(
                _timed_provider_call,
                search_flights,
                origin=orig_res.code,
                destination=dest_res.code,
                travel_date=search_req.travel_date,
                passengers=search_req.passengers,
                cabin_class=search_req.cabin_class,
            )
            fare_future = executor.submit(
                _timed_provider_call,
                ignav_client.search_one_way_fares,
                origin=orig_res.code,
                destination=dest_res.code,
                departure_date=search_req.travel_date,
                adults=search_req.passengers,
                cabin_class=search_req.cabin_class,
                max_stops=search_req.max_stops,
                preferred_airline=search_req.preferred_airline,
            )

            fare_data, fare_status, fare_duration_ms, fare_error = fare_future.result()
            schedule_data, schedule_status, schedule_duration_ms, schedule_error = schedule_future.result()
        unassociated_fare_records = list(getattr(fare_data, "unassociated_records", []))

        provider_statuses = {
            "aviationstack": schedule_status,
            "ignav": fare_status,
        }
        provider_durations_ms = {
            "aviationstack": schedule_duration_ms,
            "ignav": fare_duration_ms,
        }
        if schedule_error:
            provider_statuses["aviationstack"] = "error"
            flights_resp = {
                "status": "error",
                "message": "AviationStack schedule search failed.",
                "flights": [],
            }
        elif not isinstance(schedule_data, dict) or not isinstance(schedule_data.get("flights", []), list):
            provider_statuses["aviationstack"] = "malformed_response"
            flights_resp = {"status": "error", "message": "Malformed schedule provider response.", "flights": []}
        else:
            flights_resp = schedule_data
        if fare_error:
            fares_found = []
        elif not isinstance(fare_data, list):
            provider_statuses["ignav"] = "malformed_response"
            fares_found = []
        else:
            fares_found = []
            malformed_fare_count = 0
            for raw_fare in fare_data:
                try:
                    fares_found.append(FareResult.model_validate(raw_fare))
                except (TypeError, ValueError) as exc:
                    malformed_fare_count += 1
                    logger.warning("Ignoring malformed Ignav fare item: %s", exc)
            if malformed_fare_count:
                provider_statuses["ignav"] = (
                    "partial_response" if fares_found else "malformed_response"
                )

        # 3. OBSERVE Phase 1
        raw_flights = flights_resp["flights"]
        flight_options = []
        malformed_schedule_count = 0
        for raw_flight in raw_flights:
            try:
                flight_options.append(FlightOption.model_validate(raw_flight))
            except (TypeError, ValueError) as exc:
                malformed_schedule_count += 1
                logger.warning("Ignoring malformed schedule item: %s", exc)
        if malformed_schedule_count:
            provider_statuses["aviationstack"] = (
                "partial_response" if flight_options else "malformed_response"
            )
        obs1_desc = (
            f"AviationStack returned {len(flight_options)} flight schedule options for {orig_res.code}->{dest_res.code}. "
            f"Status: {flights_resp.get('status')}."
        )
        if flights_resp.get("status") == "future_date_unavailable":
            obs1_desc += f" Note: {flights_resp.get('message')}"
        if fare_error:
            obs1_desc += " Ignav fare search failed; schedule-only data cannot be priced."
        trace.append(AgentTraceStep(phase="OBSERVE", content=obs1_desc, observations=obs1_desc))

        # 4. ACT Phase 2: Tool 2 - compare_price()
        # Even if AviationStack plan restricts future date schedules, query Ignav fares for the exact route/date
        trace.append(AgentTraceStep(
            phase="ACT",
            content=f"Calling compare_price() to fetch authoritative fares from Ignav for {orig_res.code}->{dest_res.code} on {search_req.travel_date}.",
            tool_calls=[{
                "tool": "compare_price",
                "args": {"options_count": len(flight_options)}
            }]
        ))

        # Reuse the route/date fare response instead of making a second Ignav request.
        if flight_options:
            matched_fares = match_flights_with_fares(flight_options, fares_found)
        else:
            # When AviationStack has 0 schedules for future dates on free tier, evaluate available Ignav itineraries
            matched_fares = fares_found

        # 5. OBSERVE Phase 2
        schedule_match_count = sum(fare.schedule_matched for fare in matched_fares)
        obs2_desc = (
            f"Retrieved {len(fares_found)} live fares from Ignav. "
            f"Matched {schedule_match_count} fare(s) to schedules; evaluating {len(matched_fares)} fare candidate(s)."
        )
        trace.append(AgentTraceStep(phase="OBSERVE", content=obs2_desc, observations=obs2_desc))

        # 6. EVALUATE Phase: Hard constraints, budget filtering, and preference ranking
        eval_desc = "Evaluating candidate fares against hard constraints (exact match, date, route, budget) and user preferences."
        trace.append(AgentTraceStep(phase="EVALUATE", content=eval_desc))
        comparison_currency = search_req.currency if search_req.budget is not None else config.default_comparison_currency

        # Filter valid candidates
        is_domestic_india = (orig_res.country == "India" and dest_res.country == "India")
        currency_mismatch_count = sum(
            fare.price is not None
            and fare.price > 0
            and (not fare.currency or fare.currency.upper() != comparison_currency)
            for fare in matched_fares
        )
        if currency_mismatch_count:
            mismatch_desc = (
                f"{currency_mismatch_count} fare(s) use another currency than {comparison_currency}; comparison requires a fresh ECB rate. "
                "If conversion is unavailable, those records remain visible but cannot be budget-ranked."
            )
            trace.append(AgentTraceStep(phase="OBSERVE", content=mismatch_desc, observations=mismatch_desc))

        valid_candidates: list[FareResult] = []
        for fare in matched_fares:
            # Check price availability
            if (
                not fare.price_available
                or fare.price is None
                or fare.price <= 0
                or not math.isfinite(fare.price)
                or fare.price_status not in ("verified", "unverified")
            ):
                continue
            # Convert for budget eligibility only with a fresh attributable ECB rate.
            if not fare.currency:
                continue
            fare.comparison_amount = None
            fare.comparison_currency = comparison_currency
            fare.fx_rate_source = None
            fare.fx_observation_date = None
            fare.fx_retrieved_at = None
            try:
                comparison_amount, fx_rate = convert_fx(fare.price, fare.currency, comparison_currency)
                fare.comparison_amount = str(comparison_amount)
                fare.comparison_currency = comparison_currency
                fare.fx_status = "same_currency" if fare.currency.upper() == comparison_currency else "converted"
                fare.fx_rate_source = fx_rate.source
                fare.fx_observation_date = fx_rate.observation_date if fare.fx_status == "converted" else None
                fare.fx_retrieved_at = fx_rate.retrieved_at
            except FXUnavailable as exc:
                message = str(exc).lower()
                fare.fx_status = "stale" if "stale" in message else "unsupported" if any(x in message for x in ("unsupported", "no reference rate")) else "unavailable"
                continue
            if fare.cabin_class and re.sub(r"[\s_-]+", "", fare.cabin_class).lower() != re.sub(
                r"[\s_-]+", "", search_req.cabin_class
            ).lower():
                continue
            # Domestic filter: Foreign carriers cannot operate domestic Indian routes
            if is_domestic_india and not _is_indian_domestic_carrier(fare):
                continue
            # Check route & date
            if fare.origin.upper() != orig_res.code or fare.destination.upper() != dest_res.code:
                continue
            if fare.travel_date != search_req.travel_date and fare.itinerary_kind == "direct":
                continue
            if search_req.max_stops is not None and fare.stops > search_req.max_stops:
                continue
            # Check budget hard constraint
            if search_req.budget is not None and comparison_amount > search_req.budget:
                continue
            valid_candidates.append(fare)

        # 6b. CONNECTING FLIGHT SYNTHESIS: If no valid direct candidates exist within budget,
        # query major connecting transit hubs (DEL, BOM, BLR, HYD) to construct valid 1-stop connecting itineraries
        allow_connecting = search_req.max_stops is None or search_req.max_stops >= 1
        if not valid_candidates and allow_connecting:
            connecting_fares = self._search_connecting_hubs(
                origin=orig_res.code,
                destination=dest_res.code,
                travel_date=search_req.travel_date,
                adults=search_req.passengers,
                cabin_class=search_req.cabin_class,
                budget=search_req.budget,
                currency=comparison_currency,
            )
            if connecting_fares:
                valid_candidates.extend(connecting_fares)
                matched_fares.extend(connecting_fares)
                conn_desc = (
                    f"Synthesized {len(connecting_fares)} 1-stop self-transfer itineraries via hubs "
                    f"(DEL/BOM/BLR/HYD). Fares are per-leg; not a confirmed through-ticket."
                )
                trace.append(AgentTraceStep(phase="OBSERVE", content=conn_desc, observations=conn_desc))

        # 7. DECIDE Phase: Preference ranking and winning flight selection
        best_flight: Optional[FareResult] = None
        recommendation_reason: Optional[str] = None
        suggestions: list[str] = []

        if valid_candidates:
            valid_candidates = rank_fares(valid_candidates, search_req)
            best_flight = valid_candidates[0]

            reasons = []
            if best_flight.price_status == "verified":
                reasons.append("Ignav fare status is verified; seat availability is not confirmed")
            if search_req.budget:
                symbol = _currency_symbol(search_req.currency)
                reasons.append(f"within budget ({symbol}{best_flight.price:,.0f} <= {symbol}{search_req.budget:,.0f})")
            if search_req.preferred_airline and best_flight.airline and search_req.preferred_airline.lower() in best_flight.airline.lower():
                reasons.append(f"matches airline preference ({best_flight.airline})")
            if search_req.preferred_departure and best_flight.departure_time:
                try:
                    hour = int(best_flight.departure_time[:2])
                    time_match = (
                        (search_req.preferred_departure == "morning" and 5 <= hour < 12)
                        or (search_req.preferred_departure == "afternoon" and 12 <= hour < 17)
                        or (search_req.preferred_departure == "evening" and 17 <= hour < 21)
                        or (search_req.preferred_departure == "night" and (hour >= 21 or hour < 5))
                    )
                    if time_match:
                        reasons.append(f"matches {search_req.preferred_departure} departure preference")
                except ValueError:
                    pass
            if best_flight.stops == 0:
                reasons.append("nonstop flight")
            if best_flight.schedule_matched:
                reasons.append("matched to an AviationStack schedule")
            else:
                reasons.append("fare-only result; no matching AviationStack schedule was confirmed")
            reasons.append("highest normalized weighted preference score among eligible candidates")

            recommendation_reason = ", ".join(reasons).capitalize() + "."
            symbol = _currency_symbol(best_flight.currency or search_req.currency)
            decide_desc = f"Selected {best_flight.airline or ''} {best_flight.flight_number} at {symbol}{best_flight.price:,.0f} [{best_flight.price_status.upper()}]. Reason: {recommendation_reason}"
        else:
            # Generate actionable suggestions when no candidates meet all constraints
            priced_fares = [
                f for f in (matched_fares or fares_found)
                if f.price is not None
                and f.price > 0
                and math.isfinite(f.price)
                and f.currency
                and f.currency.upper() == comparison_currency
                and f.price > 0
                and f.currency
                and f.currency.upper() == comparison_currency
                and f.origin.upper() == orig_res.code
                and f.destination.upper() == dest_res.code
                and (f.itinerary_kind != "direct" or f.travel_date == search_req.travel_date)
            ]
            if priced_fares and search_req.budget:
                min_fare = min(priced_fares, key=lambda x: x.price if x.price else float("inf"))
                diff = (min_fare.price - search_req.budget) if min_fare.price else 0
                symbol = _currency_symbol(search_req.currency)
                decide_desc = f"No flights found within the budget of {symbol}{search_req.budget:,.0f} for {search_req.travel_date}."
                recommendation_reason = f"All available flights exceed your budget of {symbol}{search_req.budget:,.0f}."
                suggestions.append(
                    f"Budget Adjustment: The cheapest available flight is {min_fare.airline or ''} {min_fare.flight_number} at {symbol}{min_fare.price:,.0f} {search_req.currency} ({symbol}{diff:,.0f} above budget). Consider increasing your budget to {symbol}{min_fare.price:,.0f}."
                )
            else:
                decide_desc = f"No verified fares available for the requested route and date."
                recommendation_reason = (
                    f"No fares were returned in {comparison_currency}; prices in other currencies could not be compared."
                    if currency_mismatch_count
                    else "No flights matched the requested criteria."
                )

            # Nearby Airport Suggestions
            orig_hubs = NEARBY_HUBS.get(orig_res.code, [])
            dest_hubs = NEARBY_HUBS.get(dest_res.code, [])
            if orig_hubs:
                suggestions.append(f"Alternate Departure Hubs: Consider checking flights from nearby airports: {', '.join(orig_hubs)}.")
            if dest_hubs:
                suggestions.append(f"Alternate Arrival Hubs: Consider checking flights to nearby airports: {', '.join(dest_hubs)}.")

            # Date Flexibility Suggestion
            suggestions.append(f"Flexible Dates: Try searching ±1 to 3 days around {search_req.travel_date} as fares can vary significantly by weekday.")

        trace.append(AgentTraceStep(phase="DECIDE", content=decide_desc))

        # 8. BOOKING LINKS Retrieval & Verified Page Check (for winning flight)
        booking_opt: Optional[BookingOption] = None
        web_ver: Optional[ScrapingVerificationResult] = None

        if best_flight and best_flight.itinerary_kind == "connecting_self_transfer":
            web_ver = ScrapingVerificationResult(
                status="unavailable",
                details=(
                    "A self-transfer itinerary requires separate bookings, so there is no single "
                    "booking page to verify."
                ),
            )
            best_flight.web_verification_status = web_ver.status
            best_flight.web_verification_url = None
        elif best_flight:
            # 8a. Try querying Ignav booking-links API first if ignav_id is present
            if best_flight.ignav_id:
                try:
                    booking_options = ignav_client.get_booking_links(best_flight.ignav_id)
                    if booking_options:
                        airline_direct = next((b for b in booking_options if b.provider_type == "airline"), None)
                        booking_opt = airline_direct or booking_options[0]
                        best_flight.booking_url = booking_opt.url
                        best_flight.booking_provider = booking_opt.provider
                        best_flight.booking_provider_type = booking_opt.provider_type
                        best_flight.booking_link_kind = "provider_supplied"
                except Exception as e:
                    logger.error(f"Error fetching booking links from Ignav: {e}")

            # 8b. Discover search URLs if needed; optional page verification (separate from fare API)
            try:
                b_url, b_provider, b_type, web_ver, link_kind = booking_verifier.find_and_verify_booking(
                    flight=best_flight,
                    passengers=search_req.passengers,
                    cabin_class=search_req.cabin_class,
                )
                if not best_flight.booking_url or best_flight.booking_link_kind == "none":
                    best_flight.booking_url = b_url
                    best_flight.booking_provider = b_provider
                    best_flight.booking_provider_type = b_type
                    best_flight.booking_link_kind = link_kind
                best_flight.web_verification_status = web_ver.status
                best_flight.web_verification_url = web_ver.url or b_url

                if not booking_opt and b_url and b_provider:
                    booking_opt = BookingOption(
                        provider=b_provider,
                        provider_type=b_type,
                        price=best_flight.price,
                        currency=best_flight.currency,
                        url=b_url,
                    )
            except Exception as e:
                logger.error(f"Error discovering/verifying booking URL: {e}")
                web_ver = ScrapingVerificationResult(
                    status="unavailable",
                    url=best_flight.booking_url,
                    details=f"Verification unavailable: {str(e)}"
                )
                best_flight.web_verification_status = "unavailable"

        # 10. Format final response
        formatted_output = self._format_response(
            search_req=search_req,
            best_flight=best_flight,
            recommendation_reason=recommendation_reason,
            web_ver=web_ver,
            suggestions=suggestions,
        )

        # Record in memory
        self.memory.record_turn(
            user_query=user_query,
            agent_response=formatted_output,
            metadata={"flight_number": best_flight.flight_number if best_flight else None}
        )

        return FlightSearchResult(
            query=user_query,
            search_request=search_req,
            flights_found=flight_options,
            fares_found=fares_found,
            unassociated_fare_records=unassociated_fare_records,
            matched_fares=matched_fares,
            valid_candidates=valid_candidates,
            best_flight=best_flight,
            booking_option=booking_opt,
            web_verification=web_ver,
            trace=trace,
            recommendation_reason=recommendation_reason,
            suggestions=suggestions,
            formatted_response=formatted_output,
            request_id=req_id,
            provider_statuses=provider_statuses,
            provider_durations_ms=provider_durations_ms,
        )

    def _format_response(
        self,
        search_req: SearchRequest,
        best_flight: Optional[FareResult],
        recommendation_reason: Optional[str],
        web_ver: Optional[ScrapingVerificationResult],
        suggestions: Optional[list[str]] = None,
    ) -> str:
        """Format the standardized response adhering to Section 40 specification."""
        if not best_flight or best_flight.price is None:
            sugg_str = ""
            if suggestions:
                sugg_str = "\n\n💡 SMART RECOMMENDATIONS:\n" + "\n".join([f"- {s}" for s in suggestions])

            budget_line = f"Budget: {_currency_symbol(search_req.currency)}{search_req.budget:,.0f}" if search_req.budget else "Budget: None specified"
            return (
                "✈️ NO MATCHING FLIGHT FOUND\n\n"
                f"Route: {search_req.origin_city or search_req.origin_code} → {search_req.destination_city or search_req.destination_code}\n"
                f"Date: {search_req.travel_date}\n"
                f"{budget_line}\n\n"
                "Price:\nUNAVAILABLE\n\n"
                f"Reason:\n{recommendation_reason or 'No fare could be reliably matched to the exact flight within budget.'}"
                f"{sugg_str}"
            )

        budget_status = "WITHIN BUDGET" if (search_req.budget and best_flight.price <= search_req.budget) else "NO BUDGET SET"
        if search_req.budget and best_flight.price > search_req.budget:
            budget_status = "EXCEEDS BUDGET"

        link_label = {
            "provider_supplied": "Provider-supplied link (not verified)",
            "provider_verified": "Provider-confirmed booking link",
            "provider_search": "Provider search link",
            "discovered_search": "Discovered search link (not verified)",
            "none": "No booking link",
        }.get(best_flight.booking_link_kind, "Booking link")

        booking_display = (
            f"[{best_flight.booking_provider or 'Book Flight'}]({best_flight.booking_url})"
            if best_flight.booking_url
            else "Booking link was not available for this itinerary."
        )
        ver_status = (web_ver.status.upper() if web_ver else best_flight.web_verification_status.upper())
        fare_status_line = best_flight.price_status.upper()
        if best_flight.itinerary_kind == "connecting_self_transfer":
            leg_statuses = ", ".join(
                f"{leg.flight_number}: {leg.price_status.upper()}"
                for leg in (best_flight.legs or [])
            )
            fare_status_line = (
                f"SELF-TRANSFER ESTIMATE — separate bookings; combined total is not confirmed"
                f"{f' (leg fares: {leg_statuses})' if leg_statuses else ''}"
            )

        lines = [
            "✈️ BEST FLIGHT",
            "",
            f"{best_flight.airline or ''} {best_flight.flight_number}".strip(),
            "",
            f"{search_req.origin_city or best_flight.origin} ({best_flight.origin})",
            "→",
            f"{search_req.destination_city or best_flight.destination} ({best_flight.destination})",
            "",
            f"Date:\n{best_flight.travel_date}",
            "",
            f"Departure:\n{best_flight.departure_time or 'N/A'}",
            "",
            f"Arrival:\n{best_flight.arrival_time or 'N/A'}",
            "",
            f"Price:\n{_currency_symbol(best_flight.currency or search_req.currency)}{best_flight.price:,.0f}",
            "",
            f"Fare status:\n{fare_status_line}",
            "",
            f"Schedule match:\n{'MATCHED TO AVIATIONSTACK' if best_flight.schedule_matched else 'FARE ONLY — NO MATCHING SCHEDULE'}",
            "",
            "Seat availability:\nNot confirmed by the fare provider",
            "",
            f"Fare retrieved:\n{best_flight.retrieved_at or 'N/A'} ({_freshness_label(best_flight.retrieved_at)})",
            "",
            (
                f"Schedule retrieved:\n{best_flight.schedule_retrieved_at} "
                f"({_freshness_label(best_flight.schedule_retrieved_at, fresh_minutes=720)})"
                if best_flight.schedule_matched
                else "Schedule retrieved:\nNo matching schedule"
            ),
            "",
            f"Data source:\n{best_flight.source}",
            "",
            f"Budget:\n{_currency_symbol(search_req.currency)}{search_req.budget:,.0f}" if search_req.budget else "Budget:\nNot specified",
            "",
            f"Budget status:\n{budget_status}",
            "",
            f"Booking provider:\n{best_flight.booking_provider or 'Airline / OTA'}",
            "",
            f"Booking link type:\n{link_label}",
            "",
            f"Booking:\n{booking_display}",
            "",
            f"Page verification:\n{ver_status}",
            "",
            f"Why:\n{recommendation_reason or 'Cheapest verified exact-match flight within your budget.'}",
        ]
        return "\n".join(lines)

    def _search_connecting_hubs(
        self,
        origin: str,
        destination: str,
        travel_date: str,
        adults: int = 1,
        cabin_class: str = "economy",
        budget: Optional[float] = None,
        currency: str = "INR",
    ) -> list[FareResult]:
        """Synthesize 1-stop self-transfer itineraries via primary hubs (timezone-aware layovers)."""
        hubs = ["DEL", "BOM", "BLR", "HYD"]
        connecting_options: list[FareResult] = []
        next_date = (date.fromisoformat(travel_date) + timedelta(days=1)).isoformat()
        hub_futures = {}

        # Hub/date searches are independent network requests; overlap them to reduce fallback latency.
        with ThreadPoolExecutor(max_workers=8) as executor:
            for hub in hubs:
                if origin == hub or destination == hub:
                    continue
                leg1_future = executor.submit(
                    ignav_client.search_one_way_fares,
                    origin=origin,
                    destination=hub,
                    departure_date=travel_date,
                    adults=adults,
                    cabin_class=cabin_class,
                    max_stops=0,
                )
                leg2_futures = [
                    executor.submit(
                        ignav_client.search_one_way_fares,
                        origin=hub,
                        destination=destination,
                        departure_date=leg2_date,
                        adults=adults,
                        cabin_class=cabin_class,
                        max_stops=0,
                    )
                    for leg2_date in (travel_date, next_date)
                ]
                hub_futures[hub] = (leg1_future, leg2_futures)

            fares_by_hub = {}
            for hub, (leg1_future, leg2_futures) in hub_futures.items():
                try:
                    leg1_fares = leg1_future.result()
                    leg2_fares = [
                        fare
                        for future in leg2_futures
                        for fare in future.result()
                    ]
                    fares_by_hub[hub] = (leg1_fares, leg2_fares)
                except Exception as e:
                    logger.error("Error searching connecting hub %s: %s", hub, e)

        for hub in hubs:
            if hub not in fares_by_hub:
                continue

            try:
                leg1_fares, leg2_fares = fares_by_hub[hub]
                if not leg1_fares or not leg2_fares:
                    continue

                for l1 in leg1_fares:
                    for l2 in leg2_fares:
                        if (
                            l1.stops != 0
                            or l2.stops != 0
                            or l1.passengers != adults
                            or l2.passengers != adults
                            or l1.cabin_class != cabin_class
                            or l2.cabin_class != cabin_class
                        ):
                            continue
                        conn = validate_hub_connection(l1, l2, hub, travel_date)
                        if not conn.ok or conn.layover_minutes is None or conn.leg2_departure is None:
                            continue

                        total_price, sum_currency, price_note = combined_price_if_compatible(l1, l2)
                        if total_price is None:
                            continue
                        if sum_currency != currency.upper():
                            continue
                        if budget is not None and total_price > budget:
                            continue

                        combined_status = "unverified"
                        if l1.price_status == "verified" and l2.price_status == "verified":
                            combined_status = "unverified"

                        try:
                            legs = build_itinerary_legs(
                                l1, l2, hub, travel_date, conn.layover_minutes, conn.leg2_departure
                            )
                        except ValueError:
                            continue
                        airline_name = (
                            l1.airline
                            if (l1.airline and l1.airline == l2.airline)
                            else f"{l1.airline or 'Airline'} / {l2.airline or 'Airline'}"
                        )
                        note = (
                            "Separate tickets; sum of leg fares. Not a through-ticket unless airline confirms."
                        )
                        if price_note:
                            note = price_note

                        connecting_options.append(
                            FareResult(
                                flight_number=f"{l1.flight_number} + {l2.flight_number}",
                                airline=airline_name,
                                origin=origin,
                                destination=destination,
                                travel_date=travel_date,
                                departure_time=l1.departure_time,
                                arrival_time=l2.arrival_time,
                                price=total_price,
                                currency=sum_currency or currency,
                                retrieved_at=oldest_timestamp(l1.retrieved_at, l2.retrieved_at),
                                passengers=adults,
                                cabin_class=cabin_class,
                                price_status=combined_status,
                                price_available=True,
                                stops=1,
                                itinerary_kind="connecting_self_transfer",
                                connection_hub=hub,
                                layover_minutes=conn.layover_minutes,
                                legs=legs,
                                combined_price_note=note,
                                through_ticket_confirmed=False,
                                ignav_id=None,
                                booking_url=None,
                                booking_provider="Per-leg booking required",
                                booking_provider_type="airline",
                                booking_link_kind="none",
                                web_verification_status="unavailable",
                            )
                        )
            except Exception as e:
                logger.error("Error searching connecting hub %s: %s", hub, e)
                continue

        connecting_options.sort(key=lambda x: x.price if x.price else float("inf"))
        return connecting_options


# Singleton
agent = FlightOptionFinderAgent()
