"""Unit tests for requests + BeautifulSoup booking page verification."""

import pytest
import socket
from unittest.mock import patch, MagicMock
from scraping.booking_verifier import BookingVerifier
from models import FlightOption, FareResult
from scraping.url_safety import URLSafetyError


@pytest.fixture
def verifier():
    return BookingVerifier(timeout=5)


def test_scraping_no_url(verifier):
    res = verifier.verify_booking_page(None)
    assert res.status == "unavailable"
    assert res.url is None


def test_scraping_passed_verification(verifier):
    mock_html = """
    <html>
        <head><title>IndiGo Booking Confirmation</title></head>
        <body>
            <h1>Flight 6E327 - Delhi to Mumbai</h1>
            <p>Route: DEL to BOM</p>
            <p>Travel date: 2026-09-24</p>
            <p>Total Fare: ₹6,247 INR</p>
        </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.encoding = "utf-8"
    mock_resp.iter_content.return_value = [mock_html.encode("utf-8")]

    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    with patch("scraping.url_safety.socket.getaddrinfo", return_value=[(socket.AF_INET, 0, 0, "", ("8.8.8.8", 443))]):
        with patch("requests.get", return_value=mock_resp):
            res = verifier.verify_booking_page(
                booking_url="https://www.goindigo.in/booking/sample",
                expected_flight=flight,
                expected_fare=6247.0
            )
            assert res.status == "passed"
            assert res.extracted_flight_number == "6E327"
            assert res.extracted_fare == 6247.0
            assert res.extracted_date == "2026-09-24"


def test_scraping_partial_verification(verifier):
    mock_html = """
    <html>
        <head><title>IndiGo Flight Select</title></head>
        <body>
            <h1>Select flights from DEL to BOM</h1>
            <div>Loading live dynamic seat map...</div>
        </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.encoding = "utf-8"
    mock_resp.iter_content.return_value = [mock_html.encode("utf-8")]

    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    with patch("scraping.url_safety.socket.getaddrinfo", return_value=[(socket.AF_INET, 0, 0, "", ("8.8.8.8", 443))]):
        with patch("requests.get", return_value=mock_resp):
            res = verifier.verify_booking_page(
                booking_url="https://www.goindigo.in/booking/sample",
                expected_flight=flight,
                expected_fare=6247.0
            )
            assert res.status == "partial"


def test_scraping_unavailable_on_timeout(verifier):
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    with patch("scraping.url_safety.socket.getaddrinfo", return_value=[(socket.AF_INET, 0, 0, "", ("8.8.8.8", 443))]):
        with patch("requests.get", side_effect=Exception("Connection timed out")):
            res = verifier.verify_booking_page(
                booking_url="https://www.goindigo.in/booking",
                expected_flight=flight,
                expected_fare=6247.0
            )
            assert res.status == "unavailable"


def test_scraping_blocks_unapproved_redirect_before_request(verifier):
    redirect_response = MagicMock()
    redirect_response.status_code = 302
    redirect_response.headers = {"Location": "http://127.0.0.1/admin"}

    with patch(
        "scraping.booking_verifier.validate_https_booking_url",
        side_effect=["https://www.goindigo.in/booking", URLSafetyError("Only HTTPS booking URLs are permitted.")],
    ):
        with patch("requests.get", return_value=redirect_response) as get:
            result = verifier.verify_booking_page("https://www.goindigo.in/booking")

    assert result.status == "unavailable"
    assert "Redirect blocked" in result.details
    get.assert_called_once()
    assert get.call_args.kwargs["allow_redirects"] is False


def test_scraping_does_not_accept_flight_number_from_url(verifier):
    response = MagicMock()
    response.status_code = 200
    response.encoding = "utf-8"
    response.iter_content.return_value = [b"<html><body>Search for available flights.</body></html>"]

    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )
    with patch("scraping.url_safety.socket.getaddrinfo", return_value=[(socket.AF_INET, 0, 0, "", ("8.8.8.8", 443))]):
        with patch("requests.get", return_value=response):
            result = verifier.verify_booking_page(
                "https://www.goindigo.in/booking/6E327",
                expected_flight=flight,
                expected_fare=6247,
            )

    assert result.status == "unavailable"


def test_scraping_rejects_partial_numeric_fare_match(verifier):
    html = "<html><body>Flight 6E327 DEL to BOM on 2026-09-24. Total fare: INR 16247. This result page includes itinerary information.</body></html>"
    response = MagicMock()
    response.status_code = 200
    response.encoding = "utf-8"
    response.iter_content.return_value = [html.encode("utf-8")]
    flight = FlightOption(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date="2026-09-24",
    )

    with patch("scraping.url_safety.socket.getaddrinfo", return_value=[(socket.AF_INET, 0, 0, "", ("8.8.8.8", 443))]):
        with patch("requests.get", return_value=response):
            result = verifier.verify_booking_page(
                "https://www.goindigo.in/booking/sample",
                expected_flight=flight,
                expected_fare=6247,
            )

    assert result.status == "failed"
