"""Booking URL discovery and auxiliary page verification.

Uses HTTPS requests and BeautifulSoup to inspect public pages.
Discovery synthesizes airline search deep-links when the fare API omits URLs.
Verification never treats HTTP 200 or URL substrings alone as proof of availability.
"""

import re
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests

from models import FlightOption, FareResult, ScrapingVerificationResult
from scraping.url_safety import (
    URLSafetyError,
    validate_https_booking_url,
    MAX_RESPONSE_BYTES,
)

logger = logging.getLogger(__name__)


class BookingVerifier:
    """Discover booking URLs and optionally verify page content."""

    def __init__(self, timeout: int = 3):
        self.timeout = timeout

    def verify_booking_page(
        self,
        booking_url: Optional[str],
        expected_flight: Optional[FlightOption | FareResult] = None,
        expected_fare: Optional[float] = None,
    ) -> ScrapingVerificationResult:
        """Inspect a public HTTPS booking URL on an approved host."""
        if not booking_url:
            return ScrapingVerificationResult(
                status="unavailable",
                url=None,
                details="No booking URL provided for verification.",
            )

        original_url = booking_url
        try:
            validate_https_booking_url(booking_url)
        except URLSafetyError as e:
            return ScrapingVerificationResult(
                status="unavailable",
                url=booking_url,
                original_url=original_url,
                details=str(e),
            )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        try:
            current_url = booking_url
            redirect_statuses = {301, 302, 303, 307, 308}
            for redirect_count in range(6):
                response = requests.get(
                    current_url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                    stream=True,
                )

                if response.status_code not in redirect_statuses:
                    break

                location = response.headers.get("Location")
                redirect_url = urljoin(current_url, location) if location else ""
                response.close()
                try:
                    validate_https_booking_url(redirect_url)
                except URLSafetyError as e:
                    return ScrapingVerificationResult(
                        status="unavailable",
                        url=redirect_url or current_url,
                        original_url=original_url,
                        details=f"Redirect blocked: {e}",
                    )
                current_url = redirect_url
            else:
                return ScrapingVerificationResult(
                    status="unavailable",
                    url=current_url,
                    original_url=original_url,
                    details="Booking page exceeded the safe redirect limit.",
                )

            final_url = current_url

            if response.status_code != 200:
                return ScrapingVerificationResult(
                    status="unavailable",
                    url=final_url,
                    original_url=original_url,
                    details=f"Booking page returned HTTP {response.status_code}.",
                )

            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    return ScrapingVerificationResult(
                        status="unavailable",
                        url=final_url,
                        original_url=original_url,
                        details="Response exceeded safe size limit.",
                    )
                chunks.append(chunk)
            html_text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")

            if not html_text or len(html_text) < 100:
                return ScrapingVerificationResult(
                    status="unavailable",
                    url=final_url,
                    original_url=original_url,
                    details="Booking page returned empty or minimal HTML.",
                )

            soup = BeautifulSoup(html_text, "html.parser")
            page_text = soup.get_text(separator=" ", strip=True)
            page_upper = page_text.upper()

            extracted_flight_num: Optional[str] = None
            extracted_price: Optional[float] = None
            extracted_route: Optional[str] = None
            extracted_date: Optional[str] = None

            flight_matched = False
            if expected_flight and expected_flight.flight_number:
                fn = expected_flight.flight_number.upper().split("+")[0].strip()
                if fn and re.search(rf"(?<![A-Z0-9]){re.escape(fn)}(?![A-Z0-9])", page_upper):
                    flight_matched = True
                    extracted_flight_num = fn

            route_matched = False
            if expected_flight and expected_flight.origin and expected_flight.destination:
                orig = expected_flight.origin.upper()
                dest = expected_flight.destination.upper()
                route_pattern = rf"(?<![A-Z]){re.escape(orig)}\s*(?:TO|->|→|-)\s*{re.escape(dest)}(?![A-Z])"
                if re.search(route_pattern, page_upper):
                    route_matched = True
                    extracted_route = f"{orig}->{dest}"

            date_matched = False
            if expected_flight and expected_flight.travel_date:
                expected_date = expected_flight.travel_date
                date_matched = bool(re.search(rf"(?<!\d){re.escape(expected_date)}(?!\d)", page_text))
                if date_matched:
                    extracted_date = expected_date

            price_matched = False
            fare_conflict = False
            if expected_fare is not None:
                labeled_price = re.search(
                    r"(?:total\s+fare|total\s+amount|fare|price)\s*[:\-]?\s*(₹|INR\s*)?([0-9][0-9,]*(?:\.\d{1,2})?)\s*(INR)?",
                    page_text,
                    re.IGNORECASE,
                )
                if labeled_price:
                    currency_marker = (labeled_price.group(1) or "") + (labeled_price.group(3) or "")
                    amount_text = labeled_price.group(2).replace(",", "")
                    try:
                        page_amount = Decimal(amount_text)
                        expected_amount = Decimal(str(expected_fare))
                    except InvalidOperation:
                        page_amount = expected_amount = Decimal("NaN")
                    expected_currency = getattr(expected_flight, "currency", None)
                    currency_matched = (
                        bool(currency_marker.strip())
                        and (not expected_currency or expected_currency.upper() == "INR")
                    )
                    if currency_matched:
                        extracted_price = float(page_amount)
                        price_matched = page_amount == expected_amount
                        fare_conflict = not price_matched

            if flight_matched and route_matched and date_matched and price_matched:
                status = "passed"
                details = "Page text shows matching flight, route, travel date, and labeled fare evidence."
            elif flight_matched or route_matched or date_matched:
                status = "failed" if fare_conflict or (flight_matched and not route_matched) else "partial"
                details = "Static page evidence is incomplete; current booking availability is not confirmed."
            else:
                status = "unavailable"
                details = "Insufficient static evidence (dynamic render or bot protection likely)."

            return ScrapingVerificationResult(
                status=status,
                url=final_url,
                original_url=original_url,
                extracted_flight_number=extracted_flight_num,
                extracted_route=extracted_route,
                extracted_date=extracted_date,
                extracted_fare=extracted_price,
                details=details,
            )

        except requests.Timeout:
            return ScrapingVerificationResult(
                status="unavailable",
                url=booking_url,
                original_url=original_url,
                details="Timeout connecting to booking provider page.",
            )
        except Exception as e:
            logger.debug("Booking verification error: %s", e)
            return ScrapingVerificationResult(
                status="unavailable",
                url=booking_url,
                original_url=original_url,
                details="Verification unavailable due to a network or parsing error.",
            )

    def discover_booking_url(
        self,
        flight: FareResult,
        passengers: int = 1,
        cabin_class: str = "economy",
    ) -> tuple[str, str, str]:
        """Build an airline or OTA search deep-link (discovery only, not verified booking)."""
        fn = (flight.flight_number or "").upper().replace(" ", "").split("+")[0]
        orig = flight.origin.upper()
        dest = flight.destination.upper()
        date_str = flight.travel_date.strip()

        airline_name = flight.airline or "Airline"
        carrier_upper = airline_name.upper()

        if fn.startswith("AI") or "AIR INDIA" in carrier_upper:
            url = (
                f"https://www.airindia.com/in/en/book/flight-search.html?tripType=O"
                f"&origin={orig}&destination={dest}&travelDate={date_str}&adults={passengers}"
                f"&cabinClass={cabin_class.title()}"
            )
            return url, "Air India", "airline"

        if fn.startswith("6E") or "INDIGO" in carrier_upper:
            flt_digits = re.sub(r"\D", "", fn) or ""
            date_compact = date_str.replace("-", "")
            url = (
                f"https://www.goindigo.in/flight-select-redirection.html?cid=metasearch"
                f"&flightNumber={flt_digits}#{orig}/{dest}/{date_compact}/INR/{passengers}00/"
            )
            return url, "IndiGo", "airline"

        if fn.startswith("SG") or "SPICEJET" in carrier_upper:
            url = (
                f"https://www.spicejet.com/booking-search?tripType=1&origin={orig}"
                f"&destination={dest}&departure={date_str}&adults={passengers}"
            )
            return url, "SpiceJet", "airline"

        if fn.startswith("QP") or "AKASA" in carrier_upper:
            url = (
                f"https://www.akasaair.com/flight-search?origin={orig}&destination={dest}"
                f"&date={date_str}&pax={passengers}"
            )
            return url, "Akasa Air", "airline"

        if fn.startswith("UK") or "VISTARA" in carrier_upper:
            url = (
                f"https://www.airindia.com/in/en/book/flight-search.html?tripType=O"
                f"&origin={orig}&destination={dest}&travelDate={date_str}&adults={passengers}"
            )
            return url, "Air India (Vistara)", "airline"

        if fn.startswith("EK") or "EMIRATES" in carrier_upper:
            url = (
                f"https://www.emirates.com/english/book/?origin={orig}&destination={dest}"
                f"&departureDate={date_str}&adults={passengers}"
            )
            return url, "Emirates", "airline"

        if fn.startswith("QR") or "QATAR" in carrier_upper:
            url = f"https://www.qatarairways.com/en/booking.html?from={orig}&to={dest}&departDate={date_str}"
            return url, "Qatar Airways", "airline"

        if fn.startswith("BA") or "BRITISH" in carrier_upper:
            url = (
                f"https://www.britishairways.com/travel/fx/public/en_gb?eId=111011"
                f"&source=flight_search&origin={orig}&destination={dest}&depDate={date_str}"
            )
            return url, "British Airways", "airline"

        google_url = f"https://www.google.com/travel/flights?q=flights+from+{orig}+to+{dest}+on+{date_str}+{airline_name}"
        return google_url, f"{airline_name} (Google Flights)", "ota"

    def find_and_verify_booking(
        self,
        flight: FareResult,
        passengers: int = 1,
        cabin_class: str = "economy",
    ) -> tuple[str, str, str, ScrapingVerificationResult, str]:
        """Discover URL if needed, then verify page content.

        Returns: (url, provider, provider_type, verification_result, booking_link_kind)
        """
        link_kind = flight.booking_link_kind or "none"
        if flight.booking_url:
            url = flight.booking_url
            provider = flight.booking_provider or "Airline"
            p_type = flight.booking_provider_type or "airline"
            if link_kind == "none":
                link_kind = "provider_supplied"
        else:
            url, provider, p_type = self.discover_booking_url(
                flight, passengers=passengers, cabin_class=cabin_class
            )
            link_kind = "discovered_search"

        ver_result = self.verify_booking_page(url, expected_flight=flight, expected_fare=flight.price)
        return url, provider, p_type, ver_result, link_kind


booking_verifier = BookingVerifier()
