"""Unit tests for Ignav API client parsing and error handling."""

import pytest
from unittest.mock import patch, MagicMock
from ignav_client import IgnavClient, IgnavError
from models import FareResult, BookingOption


@pytest.fixture
def client():
    return IgnavClient(api_key="test_ignav_key", base_url="https://test.ignav.com/api")


def test_ignav_parse_verified_fare(client):
    mock_payload = {
        "itineraries": [
            {
                "ignav_id": "ignav_test_1",
                "price": {"amount": 6247.0, "currency": "INR", "status": "verified"},
                "outbound": {
                    "carrier": "IndiGo",
                    "segments": [
                        {
                            "marketing_carrier_code": "6E",
                            "flight_number": "327",
                            "operating_carrier_name": "IndiGo",
                            "departure_airport": "DEL",
                            "arrival_airport": "BOM",
                            "departure_time_local": "2026-09-24T08:10:00",
                            "arrival_time_local": "2026-09-24T10:20:00"
                        }
                    ]
                }
            }
        ]
    }
    fares = client._parse_fares_response(mock_payload, origin="DEL", destination="BOM", travel_date="2026-09-24")
    assert len(fares) == 1
    fare = fares[0]
    assert fare.flight_number == "6E327"
    assert fare.airline == "IndiGo"
    assert fare.airline_code == "6E"
    assert fare.origin == "DEL"
    assert fare.destination == "BOM"
    assert fare.travel_date == "2026-09-24"
    assert fare.departure_time == "08:10"
    assert fare.arrival_time == "10:20"
    assert fare.price == 6247.0
    assert fare.currency == "INR"
    assert fare.price_available is True
    assert fare.price_status == "verified"
    assert fare.provider_fields['price']['status']=='verified'
    assert fare.ignav_id == "ignav_test_1"
    assert fare.stops == 0


def test_ignav_parse_unverified_fare(client):
    mock_payload = {
        "itineraries": [
            {
                "ignav_id": "ignav_test_unverified",
                "price": {"amount": 7100.0, "currency": "INR", "status": "unverified"},
                "outbound": {
                    "carrier": "Air India",
                    "segments": [
                        {
                            "marketing_carrier_code": "AI",
                            "flight_number": "865",
                            "departure_airport": "DEL",
                            "arrival_airport": "BOM",
                            "departure_time_local": "2026-09-24T10:00:00",
                            "arrival_time_local": "2026-09-24T12:15:00"
                        }
                    ]
                }
            }
        ]
    }
    fares = client._parse_fares_response(mock_payload, origin="DEL", destination="BOM", travel_date="2026-09-24")
    assert len(fares) == 1
    assert fares[0].price_status == "unverified"
    assert fares[0].price == 7100.0


def test_ignav_preserves_route_level_fare_without_inventing_flight_number(client):
    fares=client._parse_fares_response({"itineraries":[{"id":"route-quote","price":{"amount":8000,"currency":"INR"},"outbound":{"segments":[{"departure_airport":"DEL","departure_time":"2027-01-10T09:00:00+05:30"},{"arrival_airport":"BOM","arrival_time":"2027-01-10T11:00:00+05:30"}]}}]},"DEL","BOM","2027-01-10")
    assert fares==[]
    assert len(client.unassociated_fare_records)==1
    record=client.unassociated_fare_records[0]
    assert record['association_level']=='route_level'
    assert record['origin']=='DEL' and record['destination']=='BOM'
    assert record['price']==8000 and record['currency']=='INR'
    assert record['provider_fields']['price']['amount']==8000
    assert 'flight-specific number' in record['reason']


def test_ignav_classifies_airline_level_fare_without_individual_flight(client):
    client._parse_fares_response({"itineraries":[{"id":"airline-quote","carrier":"IndiGo","price":{"amount":8000,"currency":"INR"},"outbound":{"segments":[{"departure_airport":"DEL"},{"arrival_airport":"BOM"}]}}]},"DEL","BOM","2027-01-10")
    assert client.unassociated_fare_records[0]['association_level']=='airline_level'


def test_ignav_does_not_invent_missing_currency(client):
    payload = {
        "itineraries": [{
            "flight_number": "6E327",
            "price": {"amount": 6247, "status": "verified"},
        }]
    }

    fares = client._parse_fares_response(payload, "DEL", "BOM", "2026-09-24")

    assert len(fares) == 1
    assert fares[0].currency is None
    assert fares[0].price_available is False


def test_ignav_parse_booking_links(client):
    mock_payload = {
        "booking_options": [
            {
                "legs": ["outbound"],
                "links": [
                    {
                        "provider_name": "IndiGo",
                        "provider_type": "airline",
                        "url": "https://www.goindigo.in/booking/sample"
                    }
                ]
            },
            {
                "provider": "MakeMyTrip",
                "provider_type": "ota",
                "url": "https://www.makemytrip.com/booking/sample",
                "price": 6300.0
            }
        ]
    }
    with patch.object(client, "_make_request", return_value=mock_payload):
        links = client.get_booking_links("ignav_test_1")
        assert len(links) == 2
        assert links[0].provider == "IndiGo"
        assert links[0].provider_type == "airline"
        assert links[0].url == "https://www.goindigo.in/booking/sample"
        assert links[1].provider == "MakeMyTrip"
        assert links[1].provider_type == "ota"


def test_ignav_empty_booking_links(client):
    with patch.object(client, "_make_request", return_value={"booking_options": []}):
        links = client.get_booking_links("ignav_none")
        assert links == []


def test_ignav_auth_error_raises(sample_travel_date):
    client_no_key = IgnavClient(api_key="")
    with pytest.raises(IgnavError, match="IGNAV_API_KEY is not configured"):
        client_no_key.search_one_way_fares("DEL", "BOM", sample_travel_date)


def test_ignav_rejects_non_object_success_response(client):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.text = "[]"
    mock_response.json.return_value = []
    with patch("requests.request", return_value=mock_response):
        with pytest.raises(IgnavError, match="malformed response"):
            client._make_request("POST", "fares/one-way", json_data={})


def test_ignav_validates_maximum_stops(client, sample_travel_date):
    with pytest.raises(IgnavError, match="max_stops must be between 0 and 3"):
        client.search_one_way_fares(
            "DEL",
            "BOM",
            sample_travel_date,
            max_stops=4,
        )
