"""Ignav API client for real flight fares and booking links.

Communicates with Ignav endpoints:
- POST /api/fares/one-way
- POST /api/fares/booking-links

Maintains strict provenance, preserves verified/unverified status, and handles errors cleanly.
"""

import time
import logging
import math
from typing import Any, Optional, Literal
import requests

from datetime import datetime, timezone

from config import config
from models import FareResult, BookingOption
from validation import (
    ValidationError,
    normalize_iata,
    validate_cabin_class,
    validate_passengers,
    validate_max_stops,
    validate_travel_date_not_past,
)

logger = logging.getLogger(__name__)


class FareSearchResults(list):
    """List-compatible flight fares with separately preserved non-flight fare indications."""

    def __init__(self, fares=(), unassociated_records=()):
        super().__init__(fares)
        self.unassociated_records = list(unassociated_records)


class IgnavError(Exception):
    """Base exception for Ignav API errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class IgnavClient:
    """Client for Ignav Flight Fares and Booking Links API."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else config.ignav_api_key
        self.base_url = (base_url or config.ignav_base_url).rstrip("/")
        self.unassociated_fare_records: list[dict[str, Any]] = []

    def _get_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise IgnavError("IGNAV_API_KEY is not configured.")
        return {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request to Ignav with 1 transient retry."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()

        max_attempts = 2
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    timeout=timeout if timeout is not None else getattr(config, "provider_timeout_seconds", 10),
                    verify=getattr(config, "ssl_verify", True),
                )

                if response.status_code in (401, 403):
                    raise IgnavError(
                        f"Ignav authentication/authorization failed (HTTP {response.status_code}). Check your IGNAV_API_KEY.",
                        status_code=response.status_code,
                        response_body=response.text[:500],
                    )

                if response.status_code == 400:
                    raise IgnavError(
                        f"Ignav bad request (HTTP 400): {response.text[:500]}",
                        status_code=400,
                        response_body=response.text[:500],
                    )

                if response.status_code == 429:
                    raise IgnavError(
                        "Ignav rate limit exceeded (HTTP 429).",
                        status_code=429,
                        response_body=response.text[:500],
                    )

                if response.status_code >= 500:
                    if attempt < max_attempts:
                        time.sleep(1.0)
                        continue
                    raise IgnavError(
                        f"Ignav server error (HTTP {response.status_code}).",
                        status_code=response.status_code,
                        response_body=response.text[:500],
                    )

                if not response.ok:
                    raise IgnavError(
                        f"Ignav request failed with status {response.status_code}: {response.text[:500]}",
                        status_code=response.status_code,
                        response_body=response.text[:500],
                    )

                try:
                    data = response.json()
                except ValueError as e:
                    raise IgnavError("Ignav returned invalid JSON response.", response_body=response.text[:500]) from e
                if not isinstance(data, dict):
                    raise IgnavError(
                        "Ignav returned a malformed response; expected a JSON object.",
                        response_body=response.text[:500],
                    )
                if "error" in data:
                    raise IgnavError(
                        "Ignav returned an API-level error response.",
                        status_code=response.status_code,
                    )
                return data

            except (requests.Timeout, requests.ConnectionError) as e:
                last_error = e
                if attempt < max_attempts:
                    time.sleep(1.0)
                    continue
                raise IgnavError(f"Network error connecting to Ignav: {str(e)}") from e
            except requests.RequestException as e:
                raise IgnavError(f"Ignav request failed: {str(e)}") from e

        if last_error:
            raise IgnavError(f"Ignav request failed after retries: {str(last_error)}")
        raise IgnavError("Ignav request failed unexpectedly.")

    def search_one_way_fares(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        adults: int = 1,
        cabin_class: str = "economy",
        market: str = "IN",
        max_stops: Optional[int] = None,
        preferred_airline: Optional[str] = None,
    ) -> list[FareResult]:
        """Search one-way fares using POST /api/fares/one-way."""
        try:
            origin_norm = normalize_iata(origin)
            dest_norm = normalize_iata(destination)
            date_norm = validate_travel_date_not_past(departure_date.strip(), allow_past=False)
            adults_norm = validate_passengers(adults)
            cabin_norm = validate_cabin_class(cabin_class)
            max_stops_norm = validate_max_stops(max_stops)
        except ValidationError as e:
            raise IgnavError(str(e)) from e

        payload: dict[str, Any] = {
            "origin": origin_norm,
            "destination": dest_norm,
            "departure_date": date_norm,
            "adults": adults_norm,
            "cabin_class": cabin_norm,
            "market": market.strip().upper(),
        }
        if max_stops_norm is not None:
            payload["max_stops"] = max_stops_norm
        if preferred_airline:
            payload["preferred_airline"] = preferred_airline.strip()

        data = self._make_request("POST", "fares/one-way", json_data=payload)
        retrieved_at = None
        if isinstance(data, dict):
            retrieved_at = data.get("retrieved_at") or data.get("timestamp")
        fares = self._parse_fares_response(
            data,
            origin=origin_norm,
            destination=dest_norm,
            travel_date=date_norm,
            retrieved_at=retrieved_at,
            passengers=adults_norm,
            cabin_class=cabin_norm,
        )
        return FareSearchResults(fares, self.unassociated_fare_records)

    def _format_time(self, raw_time: Optional[str]) -> Optional[str]:
        """Format ISO timestamp or time string into HH:MM format."""
        if not raw_time:
            return None
        raw = str(raw_time).strip()
        if "T" in raw:
            time_part = raw.split("T")[-1]
            return time_part[:5]
        if len(raw) >= 5 and ":" in raw:
            return raw[:5]
        return raw

    def _format_timestamp(self, raw_time: Optional[str]) -> Optional[str]:
        if not raw_time:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw_time).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.isoformat()

    def _parse_fares_response(
        self,
        data: dict[str, Any],
        origin: str,
        destination: str,
        travel_date: str,
        retrieved_at: Optional[str] = None,
        passengers: Optional[int] = None,
        cabin_class: Optional[str] = None,
    ) -> list[FareResult]:
        """Defensively parse Ignav fare response into FareResult objects."""
        results: list[FareResult] = []
        self.unassociated_fare_records = []
        if not isinstance(data, dict):
            raise IgnavError("Ignav returned a malformed fares response; expected a JSON object.")

        itineraries = data.get("itineraries") or data.get("data") or data.get("results") or []
        if isinstance(data, list):
            itineraries = data
        if not isinstance(itineraries, list):
            raise IgnavError("Ignav returned malformed fares; expected an itineraries list.")

        for item in itineraries:
            if not isinstance(item, dict):
                continue

            ignav_id = item.get("ignav_id") or item.get("id")

            # Extract price details
            price_val: Optional[float] = None
            currency_val: Optional[str] = None
            status_val: str = "unverified"

            raw_price = item.get("price")
            if isinstance(raw_price, dict):
                amount = raw_price.get("amount") or raw_price.get("total") or raw_price.get("value")
                if amount is not None:
                    try:
                        price_val = float(amount)
                        if not math.isfinite(price_val) or price_val <= 0:
                            price_val = None
                    except (ValueError, TypeError):
                        price_val = None
                currency_val = raw_price.get("currency")
                raw_status = str(raw_price.get("status") or "").lower().strip()
                if raw_status == "verified":
                    status_val = "verified"
                elif raw_status == "unverified":
                    status_val = "unverified"
                elif raw_status:
                    status_val = raw_status
            elif raw_price is not None:
                try:
                    price_val = float(raw_price)
                    if not math.isfinite(price_val) or price_val <= 0:
                        price_val = None
                except (ValueError, TypeError):
                    price_val = None
                status_val = str(item.get("status", "unverified")).lower()
                currency_val = item.get("currency")

            # Extract flight & segment details
            outbound = item.get("outbound") or {}
            if not isinstance(outbound, dict):
                logger.warning("Ignoring malformed Ignav itinerary with invalid outbound data.")
                continue
            segments = outbound.get("segments") or item.get("segments") or []
            if not isinstance(segments, list):
                logger.warning("Ignoring malformed Ignav itinerary with invalid segments.")
                continue

            airline_name = item.get("carrier") or outbound.get("carrier") or item.get("airline")
            airline_code: Optional[str] = None
            flight_number_val: Optional[str] = None
            dep_time: Optional[str] = None
            arr_time: Optional[str] = None
            dep_at: Optional[str] = None
            arr_at: Optional[str] = None
            dep_airport = origin
            arr_airport = destination
            explicit_route = False
            stops_count = len(segments) - 1 if len(segments) > 0 else 0

            if segments and isinstance(segments, list):
                first_seg = segments[0] if isinstance(segments[0], dict) else {}
                last_seg = segments[-1] if isinstance(segments[-1], dict) else {}

                airline_code = (
                    first_seg.get("marketing_carrier_code")
                    or first_seg.get("carrier_code")
                    or first_seg.get("carrier")
                )
                flight_num_only = (
                    first_seg.get("flight_number")
                    or first_seg.get("marketing_flight_number")
                    or first_seg.get("number")
                )
                if flight_num_only:
                    flight_num_str = str(flight_num_only).strip()
                    if airline_code and not flight_num_str.upper().startswith(str(airline_code).upper()):
                        flight_number_val = f"{airline_code}{flight_num_str}"
                    else:
                        flight_number_val = flight_num_str

                if not airline_name:
                    airline_name = first_seg.get("operating_carrier_name") or first_seg.get("carrier_name") or first_seg.get("airline")

                dep_airport = first_seg.get("departure_airport") or first_seg.get("origin") or dep_airport
                arr_airport = last_seg.get("arrival_airport") or last_seg.get("destination") or arr_airport
                explicit_route = bool(
                    (first_seg.get("departure_airport") or first_seg.get("origin"))
                    and (last_seg.get("arrival_airport") or last_seg.get("destination"))
                )

                raw_dep_time = first_seg.get("departure_time_local") or first_seg.get("departure_time")
                raw_arr_time = last_seg.get("arrival_time_local") or last_seg.get("arrival_time")
                dep_time = self._format_time(raw_dep_time)
                arr_time = self._format_time(raw_arr_time)
                dep_at = self._format_timestamp(raw_dep_time)
                arr_at = self._format_timestamp(raw_arr_time)

            if not flight_number_val:
                flight_number_val = item.get("flight_number") or outbound.get("flight_number")

            if not flight_number_val:
                if price_val is not None and currency_val:
                    try:
                        route_origin = normalize_iata(str(dep_airport)) if explicit_route else origin
                        route_destination = normalize_iata(str(arr_airport)) if explicit_route else destination
                        level = "route_level" if not airline_name else "airline_level"
                        raw_fields = {
                            key: item[key]
                            for key in ("price", "carrier", "airline", "outbound", "inbound", "legs", "status", "retrieved_at")
                            if key in item
                        }
                        self.unassociated_fare_records.append(
                            {
                                "association_level": level,
                                "source": "Ignav",
                                "provider_reference": str(ignav_id) if ignav_id else None,
                                "airline": str(airline_name) if airline_name else None,
                                "origin": route_origin,
                                "destination": route_destination,
                                "travel_date": dep_at[:10] if dep_at else travel_date,
                                "price": price_val,
                                "currency": str(currency_val).upper(),
                                "retrieved_at": item.get("retrieved_at") or retrieved_at,
                                "provider_fields": raw_fields,
                                "reason": (
                                    "Provider response did not include a flight-specific number; "
                                    "not eligible for flight ranking."
                                ),
                            }
                        )
                    except ValidationError:
                        pass
                continue

            try:
                dep_airport = normalize_iata(str(dep_airport))
                arr_airport = normalize_iata(str(arr_airport))
            except ValidationError as exc:
                logger.warning("Ignoring Ignav itinerary with invalid route: %s", exc)
                continue

            final_status: Literal["verified", "unverified", "unavailable", "error"] = "unverified"
            if status_val == "verified":
                final_status = "verified"
            elif status_val == "unavailable":
                final_status = "unavailable"
            elif status_val == "error":
                final_status = "error"
            else:
                final_status = "unverified"

            item_retrieved = item.get("retrieved_at") or retrieved_at
            if not item_retrieved:
                item_retrieved = datetime.now(timezone.utc).isoformat()

            booking_url = item.get("booking_url") or item.get("deep_link")
            link_kind = "none"
            if booking_url:
                link_kind = "provider_search"

            results.append(
                FareResult(
                    flight_number=str(flight_number_val).replace(" ", "").upper(),
                    association_level="itinerary_specific" if len(segments) > 1 else "flight_specific",
                    provider_fields={
                        key: item[key]
                        for key in ("price", "carrier", "airline", "outbound", "inbound", "legs", "status", "retrieved_at")
                        if key in item
                    },
                    airline=str(airline_name) if airline_name else None,
                    airline_code=str(airline_code).upper() if airline_code else None,
                    origin=str(dep_airport).upper(),
                    destination=str(arr_airport).upper(),
                    travel_date=dep_at[:10] if dep_at else travel_date,
                    departure_time=dep_time,
                    arrival_time=arr_time,
                    departure_at=dep_at,
                    arrival_at=arr_at,
                    passengers=passengers,
                    cabin_class=cabin_class,
                    price=price_val,
                    currency=str(currency_val).upper() if currency_val else None,
                    price_available=(
                        price_val is not None
                        and currency_val is not None
                        and final_status in ("verified", "unverified")
                    ),
                    price_status=final_status,
                    ignav_id=str(ignav_id) if ignav_id else None,
                    booking_url=str(booking_url) if booking_url else None,
                    booking_provider=item.get("booking_provider"),
                    booking_provider_type=item.get("booking_provider_type"),
                    booking_link_kind=link_kind if booking_url else "none",
                    source="Ignav",
                    retrieved_at=str(item_retrieved),
                    web_verification_status="unavailable",
                    stops=max(0, stops_count),
                )
            )

        return results

    def get_booking_links(self, ignav_id: str, timeout: int = 6) -> list[BookingOption]:
        """Fetch real booking links for a specific ignav_id using POST /api/fares/booking-links."""
        if not ignav_id:
            return []

        payload = {"ignav_id": str(ignav_id).strip()}
        data = self._make_request("POST", "fares/booking-links", json_data=payload, timeout=timeout)

        options: list[BookingOption] = []
        if not isinstance(data, dict):
            return options

        raw_options = (
            data.get("booking_options")
            or data.get("options")
            or data.get("links")
            or data.get("data")
            or []
        )
        if not isinstance(raw_options, list):
            raise IgnavError("Ignav returned malformed booking links; expected a list.")
        if isinstance(data, list):
            raw_options = data

        for item in raw_options:
            if not isinstance(item, dict):
                continue
            
            # Check if this item has nested 'links'
            links_list = item.get("links")
            if isinstance(links_list, list) and links_list:
                for link_entry in links_list:
                    if not isinstance(link_entry, dict):
                        continue
                    url = link_entry.get("url") or link_entry.get("link")
                    provider = (
                        link_entry.get("provider_name")
                        or link_entry.get("provider")
                        or link_entry.get("name")
                        or "Airline"
                    )
                    provider_type = link_entry.get("provider_type") or link_entry.get("type")
                    if url:
                        options.append(
                            BookingOption(
                                provider=str(provider),
                                provider_type=str(provider_type) if provider_type else None,
                                price=item.get("price"),
                                currency=item.get("currency"),
                                url=str(url),
                            )
                        )
            else:
                url = item.get("url") or item.get("booking_url") or item.get("link")
                provider = item.get("provider") or item.get("provider_name") or item.get("name") or "Airline"
                if url:
                    opt_price: Optional[float] = None
                    if "price" in item and item["price"] is not None:
                        try:
                            opt_price = float(item["price"])
                        except (ValueError, TypeError):
                            opt_price = None

                    options.append(
                        BookingOption(
                            provider=str(provider),
                            provider_type=item.get("provider_type") or item.get("type"),
                            price=opt_price,
                            currency=item.get("currency"),
                            url=str(url),
                        )
                    )

        return options


# Singleton
ignav_client = IgnavClient()
