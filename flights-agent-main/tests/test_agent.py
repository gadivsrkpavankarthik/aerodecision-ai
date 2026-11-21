"""Unit tests for Agent Orchestration and decision workflow."""

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from agent import FlightOptionFinderAgent
from memory import SessionMemory
from models import FlightOption, FareResult, BookingOption, ItineraryLeg, ScrapingVerificationResult
from llm_client import NIMError


@pytest.fixture
def agent_instance():
    mem = SessionMemory()
    return FlightOptionFinderAgent(memory=mem)


def test_agent_query_parsing(agent_instance, sample_travel_date):
    query = f"Find me a flight from Delhi to Mumbai on {sample_travel_date} under ₹8,000"
    req = agent_instance.parse_user_query(query)
    assert req.origin_city == "Delhi"
    assert req.destination_city == "Mumbai"
    assert req.travel_date == sample_travel_date
    assert req.budget == 8000.0


def test_agent_query_parses_currency(agent_instance, sample_travel_date):
    req = agent_instance.parse_user_query(
        f"Find a flight from Delhi to New York on {sample_travel_date} under $500"
    )

    assert req.budget == 500.0
    assert req.currency == "USD"

    decimal_req = agent_instance.parse_user_query(
        f"Find a flight from Delhi to New York on {sample_travel_date} under USD 500.50"
    )
    assert decimal_req.budget == 500.5
    assert decimal_req.currency == "USD"


def test_agent_keeps_unconvertible_fares_visible_but_excludes_them_from_budget(agent_instance, sample_travel_date, monkeypatch):
    from services.fx_service import FXUnavailable
    monkeypatch.setattr('agent.convert_fx',lambda *args,**kwargs: (_ for _ in ()).throw(FXUnavailable('ECB reference rate unavailable')))
    rupee_fare = FareResult(
        flight_number="AI865",
        airline="Air India",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=500.0,
        currency="INR",
        price_available=True,
        price_status="verified",
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[rupee_fare]):
            with patch.object(agent_instance, "_search_connecting_hubs", return_value=[]):
                result = agent_instance.run(
                    f"Find me a flight from Delhi to Mumbai on {sample_travel_date} under $500"
                )

    assert result.search_request.currency == "USD"
    assert result.best_flight is None
    assert result.valid_candidates == []
    assert any("fresh ECB rate" in step.content for step in result.trace)
    assert "Budget Adjustment" not in result.formatted_response
    assert "prices in other currencies could not be compared" in result.recommendation_reason
    assert result.fares_found[0].fx_status=='unavailable'


def test_agent_nim_interpretation_is_pydantic_validated(agent_instance, sample_travel_date, monkeypatch):
    monkeypatch.setattr("agent.config.nvidia_api_key", "test_key")
    response = {
        "content": (
            '{"origin_city":"Delhi","destination_city":"Mumbai",'
            f'"travel_date":"{sample_travel_date}","budget":999999,"currency":"USD"}}'
        )
    }
    query = f"Find a flight from Delhi to Mumbai on {sample_travel_date} under ₹8,000"
    with patch("agent.nim_client.chat_completion", return_value=response):
        request = agent_instance.parse_user_query(query)

    assert request.travel_date == sample_travel_date
    assert request.budget == 8000
    assert request.currency == "INR"


def test_agent_malformed_nim_output_falls_back_to_deterministic_parser(
    agent_instance, sample_travel_date, monkeypatch
):
    monkeypatch.setattr("agent.config.nvidia_api_key", "test_key")
    query = f"Find a flight from Delhi to Mumbai on {sample_travel_date}"
    with patch("agent.nim_client.chat_completion", return_value={"content": "not json"}):
        request = agent_instance.parse_user_query(query)

    assert request.origin_city == "Delhi"
    assert request.destination_city == "Mumbai"
    assert request.travel_date == sample_travel_date


def test_agent_unavailable_nim_falls_back_to_deterministic_parser(
    agent_instance, sample_travel_date, monkeypatch
):
    monkeypatch.setattr("agent.config.nvidia_api_key", "test_key")
    query = f"Find a flight from Delhi to Mumbai on {sample_travel_date}"
    with patch("agent.nim_client.chat_completion", side_effect=NIMError("service unavailable")):
        request = agent_instance.parse_user_query(query)

    assert request.origin_city == "Delhi"
    assert request.destination_city == "Mumbai"


def test_agent_keeps_fare_only_result_separate_from_schedule_match(agent_instance, sample_travel_date):
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
        price=5000,
        currency="INR",
        price_available=True,
        price_status="verified",
    )
    with patch("agent.search_flights", side_effect=TimeoutError("schedule timeout")):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[fare]):
            with patch("agent.booking_verifier.find_and_verify_booking", return_value=(
                None, None, None, ScrapingVerificationResult(status="unavailable"), "none"
            )):
                result = agent_instance.run(
                    f"Find a flight from Delhi to Mumbai on {sample_travel_date}"
                )

    assert result.best_flight is not None
    assert result.best_flight.schedule_matched is False
    assert result.provider_statuses["aviationstack"] == "error"
    assert result.provider_statuses["ignav"] == "success"
    assert "FARE ONLY — NO MATCHING SCHEDULE" in result.formatted_response
    assert "Not confirmed by the fare provider" in result.formatted_response


def test_agent_returns_provider_unavailable_status_when_both_providers_fail(agent_instance, sample_travel_date):
    with patch("agent.search_flights", side_effect=TimeoutError("schedule timeout")):
        with patch("agent.ignav_client.search_one_way_fares", side_effect=TimeoutError("fare timeout")):
            result=agent_instance.run(f"Delhi to Mumbai on {sample_travel_date}")
    assert result.provider_statuses=={"aviationstack":"error","ignav":"error"}
    assert result.best_flight is None and result.fares_found==[]
    from services.decision_service import build_decision_workspace
    build_decision_workspace(result)
    assert result.execution_status=='PROVIDER UNAVAILABLE'


def test_agent_partial_schedule_provider_failure_keeps_fare_recommendation(agent_instance, sample_travel_date):
    fare=FareResult(flight_number='6E327',airline='IndiGo',origin='DEL',destination='BOM',travel_date=sample_travel_date,price=5000,currency='INR',price_available=True,price_status='verified')
    with patch("agent.search_flights", side_effect=TimeoutError("schedule timeout")):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[fare]):
            with patch("agent.booking_verifier.find_and_verify_booking", return_value=(None,None,None,ScrapingVerificationResult(status='unavailable'),'none')):
                result=agent_instance.run(f"Delhi to Mumbai on {sample_travel_date}")
    assert result.provider_statuses['aviationstack']=='error' and result.provider_statuses['ignav']=='success'
    assert result.best_flight.flight_number=='6E327'
    assert result.best_flight.schedule_matched is False
    from services.decision_service import build_decision_workspace
    build_decision_workspace(result)
    assert result.execution_status=='COMPLETED WITH PARTIAL DATA'


def test_agent_without_budget_uses_configured_default_comparison_currency(agent_instance,sample_travel_date,monkeypatch):
    from services.fx_service import FXRate
    from config import config
    monkeypatch.setattr(config,'default_comparison_currency','EUR')
    fare=FareResult(flight_number='AI101',airline='Air India',origin='DEL',destination='BOM',travel_date=sample_travel_date,price=5000,currency='INR',price_available=True,price_status='verified')
    targets=[]
    def fx(amount,source,target):
        targets.append(target)
        return Decimal('50.00'),FXRate(Decimal('0.01'),source,target,'ECB test','2026-11-13','now','fresh')
    monkeypatch.setattr('agent.convert_fx',fx)
    with patch('agent.search_flights',return_value={'status':'success','flights':[]}):
        with patch('agent.ignav_client.search_one_way_fares',return_value=[fare]):
            with patch('agent.booking_verifier.find_and_verify_booking',return_value=(None,None,None,ScrapingVerificationResult(status='unavailable'),'none')):
                result=agent_instance.run(f'Delhi to Mumbai on {sample_travel_date}',overrides={'budget':None,'currency':'USD','passengers':1,'cabin_class':'economy','preferred_departure':None,'max_stops':None})
    assert targets and set(targets)=={'EUR'}
    assert result.best_flight.comparison_currency=='EUR'


def test_preferred_airline_can_rank_above_cheaper_fare(agent_instance, sample_travel_date):
    fares = [
        FareResult(
            flight_number="AI865",
            airline="Air India",
            origin="DEL",
            destination="BOM",
            travel_date=sample_travel_date,
            price=5000.0,
            currency="INR",
            price_available=True,
            price_status="verified",
        ),
        FareResult(
            flight_number="6E327",
            airline="IndiGo",
            origin="DEL",
            destination="BOM",
            travel_date=sample_travel_date,
            price=9000.0,
            currency="INR",
            price_available=True,
            price_status="verified",
        ),
    ]
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=fares):
            with patch("agent.booking_verifier.find_and_verify_booking", return_value=(
                None,
                None,
                None,
                ScrapingVerificationResult(status="unavailable"),
                "none",
            )):
                result = agent_instance.run(
                    f"Find me an IndiGo flight from Delhi to Mumbai on {sample_travel_date} under ₹10,000"
                )

    assert result.best_flight.flight_number == "6E327"
    assert "cheapest available option" not in result.recommendation_reason.lower()


def test_agent_rejects_unlisted_foreign_carrier_on_domestic_route(agent_instance, sample_travel_date):
    foreign_fare = FareResult(
        flight_number="TG360",
        airline="Thai Airways",
        airline_code="TG",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=5000.0,
        currency="INR",
        price_available=True,
        price_status="verified",
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[foreign_fare]):
            with patch.object(agent_instance, "_search_connecting_hubs", return_value=[]):
                result = agent_instance.run(
                    f"Find me a flight from Delhi to Mumbai on {sample_travel_date}"
                )

    assert result.best_flight is None


def test_agent_budget_rejection(agent_instance, sample_travel_date):
    """Test that flights exceeding budget are rejected."""
    expensive_fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=9500.0,  # Exceeds ₹8000
        currency="INR",
        price_available=True,
        price_status="verified",
        ignav_id="ignav_exp_1",
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[expensive_fare]):
            res = agent_instance.run(f"Find me a flight from Delhi to Mumbai on {sample_travel_date} under ₹8,000")
            assert res.best_flight is None
            assert "Price:\nUNAVAILABLE" in res.formatted_response


def test_agent_max_stops_is_a_hard_constraint(agent_instance, sample_travel_date):
    one_stop_fare = FareResult(
        flight_number="AI100",
        airline="Air India",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=5000,
        currency="INR",
        price_available=True,
        price_status="verified",
        stops=1,
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[one_stop_fare]):
            with patch.object(agent_instance, "_search_connecting_hubs", return_value=[]) as connect:
                result = agent_instance.run(
                    f"Find a flight from Delhi to Mumbai on {sample_travel_date}",
                    overrides={"max_stops": 0},
                )

    assert result.best_flight is None
    connect.assert_not_called()


def test_structured_overrides_clear_optional_session_preferences(agent_instance, sample_travel_date):
    agent_instance.memory.update(budget=100, max_stops=0)
    fare = FareResult(
        flight_number="AI100",
        airline="Air India",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=5000,
        currency="INR",
        price_available=True,
        price_status="verified",
        stops=1,
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[fare]):
            with patch("agent.booking_verifier.find_and_verify_booking", return_value=(
                None, None, None, ScrapingVerificationResult(status="unavailable"), "none"
            )):
                result = agent_instance.run(
                    f"Find a flight from Delhi to Mumbai on {sample_travel_date}",
                    overrides={
                        "budget": None,
                        "currency": "INR",
                        "passengers": 1,
                        "cabin_class": "economy",
                        "preferred_departure": None,
                        "max_stops": None,
                    },
                )

    assert result.search_request.budget is None
    assert result.search_request.max_stops is None
    assert result.best_flight is not None
    assert result.best_flight.flight_number == "AI100"


def test_agent_successful_selection(agent_instance, sample_travel_date):
    """Test full decision pipeline with a valid within-budget flight."""
    valid_fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
        arrival_time="10:20",
        price=6247.0,
        currency="INR",
        price_available=True,
        price_status="verified",
        ignav_id="ignav_test_123",
    )
    mock_booking = BookingOption(
        provider="IndiGo",
        provider_type="airline",
        price=6247.0,
        currency="INR",
        url="https://www.goindigo.in/booking/sample",
    )
    mock_ver = ScrapingVerificationResult(
        status="passed",
        url="https://www.goindigo.in/booking/sample",
        extracted_flight_number="6E327",
        extracted_fare=6247.0,
        details="Verified",
    )

    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[valid_fare]):
            with patch("agent.ignav_client.get_booking_links", return_value=[mock_booking]):
                with patch("agent.booking_verifier.verify_booking_page", return_value=mock_ver):
                    res = agent_instance.run(f"Find me a flight from Delhi to Mumbai on {sample_travel_date} under ₹8,000")
                    assert res.best_flight is not None
                    assert res.best_flight.flight_number == "6E327"
                    assert res.best_flight.price == 6247.0
                    assert res.best_flight.price_status == "verified"
                    assert res.best_flight.booking_provider == "IndiGo"
                    assert res.best_flight.web_verification_status == "passed"
                    assert "✈️ BEST FLIGHT" in res.formatted_response
                    assert "₹6,247" in res.formatted_response
                    assert "PASSED" in res.formatted_response


def test_agent_trace_completeness(agent_instance, sample_travel_date):
    """Ensure trace records PLAN, ACT, OBSERVE, EVALUATE, DECIDE."""
    valid_fare = FareResult(
        flight_number="6E327",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        price=6247.0,
        price_available=True,
        price_status="verified",
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[valid_fare]):
            res = agent_instance.run(f"Delhi to Mumbai on {sample_travel_date} under ₹8,000")
            phases = [step.phase for step in res.trace]
            assert "PLAN" in phases
            assert "ACT" in phases
            assert "OBSERVE" in phases
            assert "EVALUATE" in phases
            assert "DECIDE" in phases


def test_agent_reuses_fare_search_for_schedule_matching(agent_instance, sample_travel_date):
    schedule = FlightOption(
        flight_number="6E327",
        airline="IndiGo",
        airline_code="6E",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
        arrival_time="10:20",
    )
    fare = FareResult(
        flight_number="6E327",
        airline="IndiGo",
        airline_code="6E",
        origin="DEL",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:10",
        arrival_time="10:20",
        price=6247.0,
        currency="INR",
        price_available=True,
        price_status="verified",
    )
    with patch("agent.search_flights", return_value={"status": "success", "flights": [schedule.model_dump()]}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[fare]) as fare_search:
            with patch("agent.booking_verifier.find_and_verify_booking", return_value=(
                "https://www.goindigo.in/", "IndiGo", "airline",
                ScrapingVerificationResult(status="unavailable"), "discovered_search",
            )):
                result = agent_instance.run(
                    f"Find me a flight from Delhi to Mumbai on {sample_travel_date}"
                )

    assert fare_search.call_count == 1
    assert result.best_flight is not None
    assert result.best_flight.flight_number == "6E327"


def test_self_transfer_is_presented_as_estimate_without_single_booking_verification(
    agent_instance, sample_travel_date
):
    itinerary = FareResult(
        flight_number="AI1851 + AI2999",
        airline="Air India / Air India",
        origin="VGA",
        destination="BOM",
        travel_date=sample_travel_date,
        departure_time="08:35",
        arrival_time="00:25",
        price=46950,
        currency="INR",
        price_available=True,
        price_status="unverified",
        itinerary_kind="connecting_self_transfer",
        combined_price_note="Separate tickets; sum of leg fares.",
        legs=[
            ItineraryLeg(
                leg_index=1,
                flight_number="AI1851",
                origin="VGA",
                destination="DEL",
                travel_date=sample_travel_date,
                price=20000,
                currency="INR",
                price_status="verified",
            ),
            ItineraryLeg(
                leg_index=2,
                flight_number="AI2999",
                origin="DEL",
                destination="BOM",
                travel_date=sample_travel_date,
                price=26950,
                currency="INR",
                price_status="verified",
            ),
        ],
    )

    with patch("agent.search_flights", return_value={"status": "future_date_unavailable", "flights": []}):
        with patch("agent.ignav_client.search_one_way_fares", return_value=[]):
            with patch.object(agent_instance, "_search_connecting_hubs", return_value=[itinerary]):
                with patch("agent.booking_verifier.find_and_verify_booking") as verify_booking:
                    result = agent_instance.run(
                        f"Find a flight from Vijayawada to Mumbai on {sample_travel_date}"
                    )

    assert result.best_flight is not None
    assert result.best_flight.price_status == "unverified"
    assert result.best_flight.web_verification_status == "unavailable"
    assert result.best_flight.booking_url is None
    assert "SELF-TRANSFER ESTIMATE" in result.formatted_response
    assert "AI1851: VERIFIED, AI2999: VERIFIED" in result.formatted_response
    verify_booking.assert_not_called()
