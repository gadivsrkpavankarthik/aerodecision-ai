"""Web UI Application for T10 Flight Option Finder.

Modern light-mode design matching exact visual specifications:
- Structured Flight Form (From, To, Departure Date, Max Budget, Pax, Class, Time)
- Natural Language Prompt Tab
- Live Step-by-Step Execution Stepper with real-time animation
- Top Verified Flight Recommendation Card with direct airline booking button
- Smart Alternative Recommendations Card
- Candidate Fares & Matching Matrix Table
"""

from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Optional, Literal
import logging
import re
import uuid
import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from agent import FlightOptionFinderAgent
from analytics import SearchAnalyticsStore
from airport_resolver import resolve_airport
from memory import SessionMemoryStore
from models import FlightSearchResult, FareResult
from engines.evidence_engine import analyze_evidence
from engines.transfer_risk_engine import analyze_transfer
from engines.trip_cost_engine import estimate_trip_cost
from engines.recommendation_engine import generate_recommendations
from engines.alternative_planner import generate_alternatives
from engines.disruption_engine import simulate_disruption
from engines.readiness_engine import check_readiness
from scraping.url_safety import URLSafetyError, validate_https_booking_url
from services.trip_service import TripStore
from services.decision_service import build_decision_workspace
from validation import (
    validate_travel_date_not_past,
    validate_budget,
    validate_passengers,
    validate_cabin_class,
    validate_currency,
    validate_max_stops,
)

logger = logging.getLogger(__name__)
app = FastAPI(title="T10 Flight Option Finder")
session_memory_store = SessionMemoryStore()
analytics_store = SearchAnalyticsStore()
trip_store = TripStore(analytics_store.db_path)


class FareAnalysisPayload(BaseModel):
    fare: FareResult


class CostPayload(BaseModel):
    fares: list[FareResult]
    expenses: dict[str, Optional[float]] = Field(default_factory=dict)
    passengers: int = Field(1, ge=1, le=9)


class RecommendationsPayload(BaseModel):
    fares: list[FareResult]
    maximum_budget: Optional[float] = Field(None, gt=0)
    no_self_transfer: bool = False


class DisruptionPayload(BaseModel):
    fare: FareResult
    delay_minutes: int = Field(0, ge=0, le=1440)
    cancelled: bool = False


class ReadinessPayload(BaseModel):
    fare: FareResult
    profile: dict = Field(default_factory=dict)
    cost: Optional[dict] = None


class SaveTripPayload(BaseModel):
    title: str = Field("Saved trip", min_length=1, max_length=120)
    payload: dict


class SearchPayload(BaseModel):
    query: Optional[str] = Field(None, max_length=1000)
    origin: Optional[str] = Field(None, min_length=2, max_length=120)
    destination: Optional[str] = Field(None, min_length=2, max_length=120)
    travel_date: Optional[str] = None
    budget: Optional[float] = Field(None, gt=0, le=10000000)
    currency: str = "INR"
    passengers: int = Field(1, ge=1, le=9)
    cabin_class: str = "economy"
    preferred_departure: Optional[Literal["morning", "afternoon", "evening", "night"]] = None
    max_stops: Optional[int] = Field(None, ge=0, le=3)

    @field_validator("query", "origin", "destination")
    @classmethod
    def _trim_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("travel_date")
    @classmethod
    def _future_date(cls, value: Optional[str]) -> Optional[str]:
        return validate_travel_date_not_past(value) if value else None

    @field_validator("budget")
    @classmethod
    def _valid_budget(cls, value: Optional[float]) -> Optional[float]:
        return validate_budget(value)

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, value: str) -> str:
        return validate_currency(value)

    @field_validator("passengers")
    @classmethod
    def _valid_passengers(cls, value: int) -> int:
        return validate_passengers(value)

    @field_validator("cabin_class")
    @classmethod
    def _valid_cabin(cls, value: str) -> str:
        return validate_cabin_class(value)

    @field_validator("max_stops")
    @classmethod
    def _valid_max_stops(cls, value: Optional[int]) -> Optional[int]:
        return validate_max_stops(value)

    @model_validator(mode="after")
    def _require_search_input(self) -> "SearchPayload":
        structured_fields = (self.origin, self.destination, self.travel_date)
        if any(structured_fields) and not all(structured_fields):
            raise ValueError("origin, destination, and travel_date must be supplied together.")
        if not self.query and not all(structured_fields):
            raise ValueError("Provide a query or a complete structured flight search.")
        return self


class APIError(BaseModel):
    code: str
    message: str
    request_id: str


class APIErrorResponse(BaseModel):
    error: APIError


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    incoming_id = request.headers.get("x-request-id", "")
    request_id = incoming_id if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", incoming_id) else str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _is_safe_http_url(value: Optional[str]) -> bool:
    """Allow only HTTPS links to trusted flight booking domains."""
    if not value:
        return False
    try:
        parsed = __import__("urllib.parse").parse.urlparse(value)
    except Exception:
        return False
    if parsed.scheme.lower() not in {"https"}:
        return False
    if not parsed.netloc:
        return False
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not host:
        return False
    approved_hosts = {
        "www.goindigo.in",
        "goindigo.in",
        "www.airindia.com",
        "airindia.com",
        "www.spicejet.com",
        "spicejet.com",
        "www.akasaair.com",
        "akasaair.com",
        "www.emirates.com",
        "emirates.com",
        "www.qatarairways.com",
        "qatarairways.com",
        "www.britishairways.com",
        "britishairways.com",
        "www.google.com",
        "google.com",
    }
    return host in approved_hosts


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AeroDecision AI — Flight Option Finder</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; }
        code, pre { font-family: 'JetBrains Mono', monospace; }
        
        .badge-verified { background: #dcfce7; color: #15803d; border: 1px solid #86efac; }
        .badge-unverified { background: #fef9c3; color: #a16207; border: 1px solid #fde047; }
        .badge-unavailable { background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }
        .badge-passed { background: #dbeafe; color: #1d4ed8; border: 1px solid #93c5fd; }

        @keyframes pulse-glow {
            0%, 100% { box-shadow: 0 0 10px rgba(37, 99, 235, 0.3); }
            50% { box-shadow: 0 0 20px rgba(37, 99, 235, 0.6); }
        }
        .live-active-node {
            animation: pulse-glow 1.5s infinite;
            border-color: #2563eb !important;
            background-color: #eff6ff !important;
        }

        .step-transition {
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
    </style>
</head>
<body class="bg-slate-50/60 min-h-screen text-slate-800 antialiased selection:bg-blue-600 selection:text-white">
    <div class="max-w-5xl mx-auto px-4 py-8">
        
        <!-- Header -->
        <div class="text-center mb-8">
            <div class="inline-flex items-center justify-center p-3 bg-blue-600 text-white rounded-2xl shadow-lg shadow-blue-500/20 mb-3">
                <svg class="w-7 h-7" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path>
                </svg>
            </div>
            <h1 class="text-3xl font-black text-slate-900 tracking-tight">AeroDecision AI</h1>
            <p class="text-slate-500 mt-1.5 text-xs sm:text-sm max-w-xl mx-auto font-medium">Explainable flight decisions: live fares via Ignav, schedules via AviationStack, independent verification, transparent scoring. <span class="text-slate-400">T10 Flight Option Finder pipeline.</span></p>
        </div>

        <!-- Main Search Card (Exact Light-Theme Design) -->
        <div class="bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6 mb-8">
            <!-- Tabs -->
            <div class="flex border-b border-slate-100 pb-3 mb-5 gap-8 text-xs font-bold uppercase tracking-wider">
                <button id="tabFormBtn" onclick="switchTab('form')" class="text-blue-600 border-b-2 border-blue-600 pb-2 -mb-3 transition">Structured Flight Form</button>
                <button id="tabPromptBtn" onclick="switchTab('prompt')" class="text-slate-400 hover:text-slate-600 border-b-2 border-transparent pb-2 -mb-3 transition">Natural Language Prompt</button>
            </div>

            <!-- TAB 1: Structured Form -->
            <div id="formTabContent" class="space-y-4">
                <!-- Top Row (4 Columns) -->
                <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
                    <!-- From -->
                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">FROM (ORIGIN)</label>
                        <input type="text" id="formOrigin" list="airportList" 
                            class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-800 text-sm bg-white font-medium shadow-sm transition"
                            placeholder="e.g. London (LHR), Delhi (DEL)" value="Chennai (MAA)" required>
                    </div>

                    <!-- To -->
                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">TO (DESTINATION)</label>
                        <input type="text" id="formDest" list="airportList" 
                            class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-800 text-sm bg-white font-medium shadow-sm transition"
                            placeholder="e.g. New York (JFK), Dubai (DXB)" value="Delhi (DEL)" required>
                    </div>

                    <!-- Date -->
                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">DEPARTURE DATE</label>
                        <input type="date" id="formDate" 
                            class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-800 text-sm bg-white font-medium shadow-sm transition"
                            value="__DEFAULT_DATE__" min="__TODAY__" required>
                    </div>

                    <!-- Budget -->
                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">MAX BUDGET</label>
                        <div class="flex gap-2">
                            <input type="number" id="formBudget" 
                                class="w-full min-w-0 px-3.5 py-2.5 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-800 text-sm bg-white font-medium shadow-sm transition"
                                placeholder="25000" value="25000" min="0.01" step="0.01">
                            <select id="formCurrency" aria-label="Budget currency"
                                class="px-2.5 py-2.5 rounded-xl border border-slate-200 text-slate-800 text-sm bg-white font-medium shadow-sm">
                                <option value="INR" selected>INR</option>
                                <option value="USD">USD</option>
                                <option value="EUR">EUR</option>
                                <option value="GBP">GBP</option>
                                <option value="AED">AED</option>
                                <option value="SGD">SGD</option>
                                <option value="AUD">AUD</option>
                                <option value="CAD">CAD</option>
                            </select>
                        </div>
                    </div>
                </div>

                <!-- Secondary Row (3 Columns) -->
                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 pt-1">
                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">PASSENGERS</label>
                        <select id="formPax" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-slate-800 text-sm bg-white font-medium shadow-sm">
                            <option value="1" selected>1 Passenger</option>
                            <option value="2">2 Passengers</option>
                            <option value="3">3 Passengers</option>
                            <option value="4">4+ Passengers</option>
                        </select>
                    </div>

                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">CABIN CLASS</label>
                        <select id="formCabin" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-slate-800 text-sm bg-white font-medium shadow-sm">
                            <option value="economy" selected>Economy</option>
                            <option value="premium_economy">Premium Economy</option>
                            <option value="business">Business</option>
                        </select>
                    </div>

                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">DEPARTURE TIME</label>
                        <select id="formTime" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-slate-800 text-sm bg-white font-medium shadow-sm">
                            <option value="" selected>Any Time</option>
                            <option value="morning">Morning (05:00 - 12:00)</option>
                            <option value="afternoon">Afternoon (12:00 - 17:00)</option>
                            <option value="evening">Evening (17:00 - 21:00)</option>
                            <option value="night">Night (21:00 - 05:00)</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">MAXIMUM STOPS</label>
                        <select id="formMaxStops" class="w-full px-3.5 py-2.5 rounded-xl border border-slate-200 text-slate-800 text-sm bg-white font-medium shadow-sm">
                            <option value="" selected>Any</option>
                            <option value="0">Nonstop</option>
                            <option value="1">1 stop</option>
                            <option value="2">2 stops</option>
                            <option value="3">3 stops</option>
                        </select>
                    </div>
                </div>
            </div>

            <!-- TAB 2: Natural Language Prompt -->
            <div id="promptTabContent" class="hidden space-y-2">
                <label class="block text-[11px] font-bold text-slate-500 uppercase tracking-wider mb-1.5">Type flight request naturally</label>
                <textarea id="promptInput" rows="3" 
                    class="w-full px-4 py-3 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500 text-slate-800 text-sm bg-white font-medium shadow-sm"
                    placeholder="e.g. Find me a flight from Delhi to Dubai on September 24 under ₹20,000">Find me a flight from Delhi to Dubai on September 24 under ₹20,000</textarea>
            </div>

            <!-- Bottom Action Bar -->
            <div class="mt-5 pt-4 border-t border-slate-100 flex flex-col sm:flex-row items-center justify-between gap-4">
                <!-- Quick Chips -->
                <div class="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <span class="font-medium text-slate-400">Quick:</span>
                    <button onclick="setFormDirect('Delhi (DEL)', 'Dubai (DXB)', defaultTravelDate(), 20000)" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium rounded-lg transition">Delhi &rarr; Dubai</button>
                    <button onclick="setFormDirect('London (LHR)', 'New York (JFK)', defaultTravelDate(), 50000)" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium rounded-lg transition">London &rarr; New York</button>
                    <button onclick="setFormDirect('Chennai (MAA)', 'Delhi (DEL)', defaultTravelDate(), 25000)" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium rounded-lg transition">Chennai &rarr; Delhi</button>
                    <button onclick="setFormDirect('Vijayawada (VGA)', 'Mumbai (BOM)', defaultTravelDate(), 8000)" class="px-2.5 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium rounded-lg transition">Vijayawada &rarr; Mumbai</button>
                </div>

                <button id="searchBtn" onclick="runSearch()" 
                    class="w-full sm:w-auto px-8 py-3 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-xl text-sm shadow-md shadow-blue-500/20 transition flex items-center justify-center gap-2">
                    <span id="btnText">Find Best Option</span>
                    <svg id="btnSpinner" class="hidden animate-spin h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                </button>
            </div>
        </div>

        <!-- Live Step-by-Step Stepper Card (Animated) -->
        <div id="stepperContainer" class="hidden bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6 mb-8">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider flex items-center gap-2">
                    <span class="flex h-2 w-2 relative">
                        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
                        <span class="relative inline-flex rounded-full h-2 w-2 bg-blue-600"></span>
                    </span>
                    Agent Decision & Verification Flow
                </h3>
                <span id="stepperStatusBadge" class="text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200">EXECUTING</span>
            </div>
            <div id="stepperStepsList" class="space-y-2.5"></div>
        </div>

        <!-- Results Area -->
        <div id="resultsContainer" class="space-y-6">
            <!-- Provider Status Banner -->
            <div id="providerStatusBanner" class="hidden rounded-2xl border p-4 text-xs"></div>

            <!-- Decision Workspace Card (evidence, cost, risk, recommendations) -->
            <div id="decisionWorkspaceCard" class="hidden bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6"></div>

            <!-- Saved Trips / History -->
            <div id="savedTrips" class="bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6"></div>

            <!-- Analytics & Insights -->
            <div id="analyticsPanel" class="bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6">
                <div class="flex items-center justify-between mb-4">
                    <div>
                        <h2 class="text-lg font-bold text-slate-800">Analytics &amp; Insights</h2>
                        <p class="text-xs text-slate-500">Computed from real stored searches, provider events and interactions — never estimated.</p>
                    </div>
                    <button onclick="loadAnalytics()" class="px-4 py-2 bg-slate-800 text-white text-xs font-semibold rounded-lg hover:bg-slate-700 transition">Refresh analytics</button>
                </div>
                <div id="analyticsBody" class="text-sm text-slate-600">Loading…</div>
            </div>

            <!-- Best Option Card -->
            <div id="bestOptionCard" class="bg-white rounded-2xl border border-slate-200/90 shadow-sm overflow-hidden">
                <div class="p-8 text-center text-slate-400 text-sm">
                    Select origin, destination and date above to find the best verified flight.
                </div>
            </div>

            <!-- Smart Fallback Suggestions (When no flights meet constraints) -->
            <div id="suggestionsCard" class="hidden bg-amber-50 rounded-2xl border border-amber-200 p-6 shadow-sm">
                <div class="flex items-center gap-2 text-amber-900 font-bold text-sm mb-3">
                    <svg class="w-5 h-5 text-amber-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                    Smart Alternative Recommendations
                </div>
                <ul id="suggestionsList" class="space-y-2 text-xs text-amber-800 font-medium"></ul>
            </div>

            <!-- Request Extraction Breakdown -->
            <div class="bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6">
                <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-4">Request Extraction</h3>
                <div id="requestBreakdown" class="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm font-medium"></div>
            </div>

            <!-- Agent Trace Log -->
            <div class="bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6">
                <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-4">Agent Execution Trace (PLAN &rarr; ACT &rarr; OBSERVE &rarr; EVALUATE &rarr; DECIDE)</h3>
                <div id="traceSteps" class="space-y-2.5"></div>
            </div>

            <!-- Candidate Fares Table -->
            <div class="bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6 overflow-x-auto">
                <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
                    <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider">Candidate Fares & Real Matching Status</h3>
                    <div class="flex flex-wrap gap-2 text-[11px]">
                        <select id="sortSelect" onchange="renderCandidateTable()" class="px-2 py-1.5 rounded-lg border border-slate-200 text-slate-700 bg-white">
                            <option value="score">Sort: Best score</option>
                            <option value="price">Sort: Price</option>
                            <option value="departure">Sort: Departure</option>
                            <option value="duration">Sort: Duration</option>
                            <option value="stops">Sort: Stops</option>
                        </select>
                        <select id="filterStops" onchange="renderCandidateTable()" class="px-2 py-1.5 rounded-lg border border-slate-200 text-slate-700 bg-white">
                            <option value="">All stops</option>
                            <option value="0">Nonstop</option>
                            <option value="1">1 stop</option>
                            <option value="2">2+ stops</option>
                        </select>
                        <input id="filterAirline" oninput="renderCandidateTable()" placeholder="Filter airline" class="px-2 py-1.5 rounded-lg border border-slate-200 text-slate-700 w-32">
                        <select id="filterCabin" onchange="renderCandidateTable()" class="px-2 py-1.5 rounded-lg border border-slate-200 text-slate-700 bg-white">
                            <option value="">All cabins</option>
                            <option value="economy">Economy</option>
                            <option value="premium_economy">Premium economy</option>
                            <option value="business">Business</option>
                            <option value="first">First</option>
                        </select>
                        <input id="filterBudget" oninput="renderCandidateTable()" placeholder="Max price" type="number" class="px-2 py-1.5 rounded-lg border border-slate-200 text-slate-700 w-24">
                        <button onclick="compareShortlist()" class="px-3 py-1.5 rounded-lg bg-blue-600 text-white font-semibold hover:bg-blue-700">Compare shortlist</button>
                    </div>
                </div>
                <table class="w-full text-left text-xs text-slate-600">
                    <thead class="bg-slate-50 text-slate-700 uppercase font-semibold border-b border-slate-200 text-[11px]">
                        <tr>
                            <th class="px-4 py-3"></th>
                            <th class="px-4 py-3">Flight</th>
                            <th class="px-4 py-3">Airline</th>
                            <th class="px-4 py-3">Route</th>
                            <th class="px-4 py-3">Schedule</th>
                            <th class="px-4 py-3">Price</th>
                            <th class="px-4 py-3">Score</th>
                            <th class="px-4 py-3">Fare Status</th>
                            <th class="px-4 py-3">Schedule Match</th>
                            <th class="px-4 py-3">Verification</th>
                        </tr>
                    </thead>
                    <tbody id="candidatesTableBody" class="divide-y divide-slate-100 font-mono text-[11px]">
                        <tr>
                            <td colspan="10" class="px-4 py-6 text-center text-slate-400 font-sans italic">No fare search executed yet.</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Shortlist Comparison -->
            <div id="comparePanel" class="hidden bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6 overflow-x-auto"></div>
        </div>
    </div>

    <!-- Global & Indian Regional Airport Autocomplete Datalist -->
    <datalist id="airportList">
        <!-- North & Central India -->
        <option value="Amritsar (ATQ)">Amritsar (ATQ) - Sri Guru Ram Dass Jee Intl</option>
        <option value="Delhi (DEL)">Delhi (DEL) - Indira Gandhi Intl</option>
        <option value="Chandigarh (IXC)">Chandigarh (IXC) - Shaheed Bhagat Singh</option>
        <option value="Dehradun (DED)">Dehradun (DED) - Jolly Grant Airport</option>
        <option value="Ayodhya (AYJ)">Ayodhya (AYJ) - Maharishi Valmiki Intl</option>
        <option value="Lucknow (LKO)">Lucknow (LKO) - Chaudhary Charan Singh Intl</option>
        <option value="Varanasi (VNS)">Varanasi (VNS) - Lal Bahadur Shastri Intl</option>
        <option value="Jaipur (JAI)">Jaipur (JAI) - Jaipur Intl</option>
        <option value="Udaipur (UDR)">Udaipur (UDR) - Maharana Pratap Airport</option>
        <option value="Jodhpur (JDH)">Jodhpur (JDH) - Jodhpur Airport</option>
        <option value="Jaisalmer (JSA)">Jaisalmer (JSA) - Jaisalmer Airport</option>
        <option value="Bikaner (BKB)">Bikaner (BKB) - Nal Airport</option>
        <option value="Kishangarh (KQH)">Kishangarh (KQH) - Ajmer Airport</option>
        <option value="Dharamshala (DHM)">Dharamshala (DHM) - Kangra Airport</option>
        <option value="Kullu (KUU)">Kullu Manali (KUU) - Bhuntar Airport</option>
        <option value="Shimla (SLV)">Shimla (SLV) - Jubbarhatti Airport</option>
        <option value="Pantnagar (PGH)">Pantnagar (PGH) - Nainital Airport</option>
        <option value="Srinagar (SXR)">Srinagar (SXR) - Sheikh ul-Alam Intl</option>
        <option value="Jammu (IXJ)">Jammu (IXJ) - Jammu Civil Enclave</option>
        <option value="Leh (IXL)">Leh (IXL) - Kushok Bakula Rimpochee</option>
        <option value="Prayagraj (IXD)">Prayagraj (IXD) - Allahabad Airport</option>
        <option value="Kanpur (KNU)">Kanpur (KNU) - Chakeri Airport</option>
        <option value="Agra (AGR)">Agra (AGR) - Kheria Airport</option>
        <option value="Gorakhpur (GOP)">Gorakhpur (GOP) - Gorakhpur Airport</option>
        <option value="Bareilly (BEK)">Bareilly (BEK) - Bareilly Airport</option>
        <option value="Kushinagar (KBK)">Kushinagar (KBK) - Kushinagar Intl</option>
        <option value="Gwalior (GWL)">Gwalior (GWL) - Rajmata Vijaya Raje</option>
        <option value="Bhopal (BHO)">Bhopal (BHO) - Raja Bhoj Airport</option>
        <option value="Indore (IDR)">Indore (IDR) - Devi Ahilya Bai Holkar</option>
        <option value="Jabalpur (JLR)">Jabalpur (JLR) - Dumna Airport</option>
        <option value="Khajuraho (HJR)">Khajuraho (HJR) - Khajuraho Airport</option>
        <option value="Raipur (RPR)">Raipur (RPR) - Swami Vivekananda</option>
        <option value="Bilaspur (PAB)">Bilaspur (PAB) - Bilasa Devi Kevat</option>
        <option value="Jagdalpur (JGB)">Jagdalpur (JGB) - Maa Danteshwari</option>

        <!-- West India -->
        <option value="Mumbai (BOM)">Mumbai (BOM) - Chhatrapati Shivaji Intl</option>
        <option value="Pune (PNQ)">Pune (PNQ) - Pune Airport</option>
        <option value="Nagpur (NAG)">Nagpur (NAG) - Dr. Babasaheb Ambedkar Intl</option>
        <option value="Aurangabad (IXU)">Aurangabad (IXU) - Chhatrapati Sambhajinagar</option>
        <option value="Shirdi (SAG)">Shirdi (SAG) - Shirdi Airport</option>
        <option value="Nashik (ISK)">Nashik (ISK) - Ozar Airport</option>
        <option value="Kolhapur (KLH)">Kolhapur (KLH) - Chhatrapati Rajaram</option>
        <option value="Sindhudurg (SDW)">Sindhudurg (SDW) - Chipi Airport</option>
        <option value="Nanded (NDC)">Nanded (NDC) - Shri Guru Gobind Singh</option>
        <option value="Jalgaon (JLG)">Jalgaon (JLG) - Jalgaon Airport</option>
        <option value="Goa (GOI)">Goa Dabolim (GOI)</option>
        <option value="Goa (GOX)">Goa Mopa (GOX) - Manohar Intl</option>
        <option value="Ahmedabad (AMD)">Ahmedabad (AMD) - Sardar Vallabhbhai Patel</option>
        <option value="Surat (STV)">Surat (STV) - Surat Intl</option>
        <option value="Vadodara (BDQ)">Vadodara (BDQ) - Vadodara Airport</option>
        <option value="Rajkot (RAJ)">Rajkot (RAJ) - Hirasar Intl</option>
        <option value="Bhavnagar (BHU)">Bhavnagar (BHU) - Bhavnagar Airport</option>
        <option value="Jamnagar (JGA)">Jamnagar (JGA) - Jamnagar Airport</option>
        <option value="Porbandar (PBD)">Porbandar (PBD) - Porbandar Airport</option>
        <option value="Bhuj (BHJ)">Bhuj (BHJ) - Bhuj Airport</option>
        <option value="Kandla (IXY)">Kandla (IXY) - Gandhidham Airport</option>
        <option value="Keshod (IXK)">Keshod (IXK) - Junagadh / Gir</option>

        <!-- South India -->
        <option value="Bengaluru (BLR)">Bengaluru (BLR) - Kempegowda Intl</option>
        <option value="Chennai (MAA)">Chennai (MAA) - Chennai Intl</option>
        <option value="Hyderabad (HYD)">Hyderabad (HYD) - Rajiv Gandhi Intl</option>
        <option value="Vijayawada (VGA)">Vijayawada (VGA) - Vijayawada Intl</option>
        <option value="Visakhapatnam (VTZ)">Visakhapatnam (VTZ) - Vizag Airport</option>
        <option value="Tirupati (TIR)">Tirupati (TIR) - Tirupati Airport</option>
        <option value="Rajahmundry (RJA)">Rajahmundry (RJA) - Rajamahendravaram</option>
        <option value="Kadapa (CDP)">Kadapa (CDP) - Cuddapah Airport</option>
        <option value="Kurnool (KJB)">Kurnool (KJB) - Orvakal Airport</option>
        <option value="Kochi (COK)">Kochi (COK) - Cochin Intl</option>
        <option value="Thiruvananthapuram (TRV)">Thiruvananthapuram (TRV) - Trivandrum Intl</option>
        <option value="Kozhikode (CCJ)">Kozhikode (CCJ) - Calicut Intl</option>
        <option value="Kannur (CNN)">Kannur (CNN) - Kannur Intl</option>
        <option value="Mangalore (IXE)">Mangalore (IXE) - Mangaluru Intl</option>
        <option value="Hubli (HBX)">Hubli (HBX) - Hubballi Airport</option>
        <option value="Belgaum (IXG)">Belgaum (IXG) - Belagavi Airport</option>
        <option value="Mysore (MYQ)">Mysore (MYQ) - Mandakalli Airport</option>
        <option value="Kalaburagi (GBI)">Kalaburagi (GBI) - Gulbarga Airport</option>
        <option value="Shivamogga (RQY)">Shivamogga (RQY) - Kuvempu Airport</option>
        <option value="Bidar (IXX)">Bidar (IXX) - Bidar Airport</option>
        <option value="Ballari (VDY)">Ballari (VDY) - Jindal Vidyanagar</option>
        <option value="Coimbatore (CJB)">Coimbatore (CJB) - Coimbatore Intl</option>
        <option value="Madurai (IXM)">Madurai (IXM) - Madurai Airport</option>
        <option value="Tiruchirappalli (TRZ)">Tiruchirappalli (TRZ) - Trichy Intl</option>
        <option value="Tuticorin (TCR)">Tuticorin (TCR) - Thoothukudi Airport</option>
        <option value="Salem (SXV)">Salem (SXV) - Salem Airport</option>
        <option value="Puducherry (PNY)">Puducherry (PNY) - Pondicherry Airport</option>
        <option value="Agatti (AGX)">Agatti Island (AGX) - Lakshadweep</option>
        <option value="Port Blair (IXZ)">Port Blair (IXZ) - Veer Savarkar Intl</option>

        <!-- East & Northeast India -->
        <option value="Kolkata (CCU)">Kolkata (CCU) - Netaji Subhash Chandra Bose</option>
        <option value="Bagdogra (IXB)">Bagdogra (IXB) - Siliguri / Darjeeling</option>
        <option value="Durgapur (RDP)">Durgapur (RDP) - Kazi Nazrul Islam</option>
        <option value="Patna (PAT)">Patna (PAT) - Jay Prakash Narayan</option>
        <option value="Gaya (GAY)">Gaya (GAY) - Bodh Gaya Airport</option>
        <option value="Darbhanga (DBR)">Darbhanga (DBR) - Darbhanga Airport</option>
        <option value="Ranchi (IXR)">Ranchi (IXR) - Birsa Munda Airport</option>
        <option value="Deoghar (DGH)">Deoghar (DGH) - Deoghar Airport</option>
        <option value="Jamshedpur (IXW)">Jamshedpur (IXW) - Sonari Airport</option>
        <option value="Bhubaneswar (BBI)">Bhubaneswar (BBI) - Biju Patnaik Intl</option>
        <option value="Jharsuguda (JRG)">Jharsuguda (JRG) - Veer Surendra Sai</option>
        <option value="Rourkela (RRK)">Rourkela (RRK) - Rourkela Airport</option>
        <option value="Guwahati (GAU)">Guwahati (GAU) - Lokpriya Gopinath Bordoloi</option>
        <option value="Dibrugarh (DIB)">Dibrugarh (DIB) - Mohanbari Airport</option>
        <option value="Silchar (IXS)">Silchar (IXS) - Kumbhirgram Airport</option>
        <option value="Jorhat (JRH)">Jorhat (JRH) - Rowriah Airport</option>
        <option value="Agartala (IXA)">Agartala (IXA) - Maharaja Bir Bikram</option>
        <option value="Imphal (IMF)">Imphal (IMF) - Bir Tikendrajit Intl</option>
        <option value="Dimapur (DMU)">Dimapur (DMU) - Nagaland Airport</option>
        <option value="Aizawl (AJL)">Aizawl (AJL) - Lengpui Airport</option>
        <option value="Shillong (SHL)">Shillong (SHL) - Umroi Airport</option>
        <option value="Itanagar (HGI)">Itanagar (HGI) - Donyi Polo Airport</option>
        <option value="Pakyong (PYG)">Pakyong (PYG) - Sikkim Airport</option>

        <!-- Middle East -->
        <option value="Dubai (DXB)">Dubai (DXB) - Dubai International</option>
        <option value="Abu Dhabi (AUH)">Abu Dhabi (AUH) - Zayed Intl</option>
        <option value="Sharjah (SHJ)">Sharjah (SHJ) - Sharjah Intl</option>
        <option value="Doha (DOH)">Doha (DOH) - Hamad International</option>
        <option value="Jeddah (JED)">Jeddah (JED) - King Abdulaziz Intl</option>
        <option value="Riyadh (RUH)">Riyadh (RUH) - King Khalid Intl</option>
        <option value="Bahrain (BAH)">Bahrain (BAH) - Bahrain Intl</option>
        <option value="Kuwait (KWI)">Kuwait (KWI) - Kuwait Intl</option>
        <option value="Muscat (MCT)">Muscat (MCT) - Muscat Intl</option>

        <!-- Europe -->
        <option value="London (LHR)">London (LHR) - Heathrow Airport</option>
        <option value="London (LGW)">London (LGW) - Gatwick Airport</option>
        <option value="Manchester (MAN)">Manchester (MAN) - Manchester Airport</option>
        <option value="Edinburgh (EDI)">Edinburgh (EDI) - Edinburgh Airport</option>
        <option value="Paris (CDG)">Paris (CDG) - Charles de Gaulle</option>
        <option value="Paris (ORY)">Paris (ORY) - Paris Orly</option>
        <option value="Frankfurt (FRA)">Frankfurt (FRA) - Frankfurt Airport</option>
        <option value="Munich (MUC)">Munich (MUC) - Munich Airport</option>
        <option value="Berlin (BER)">Berlin (BER) - Berlin Brandenburg</option>
        <option value="Amsterdam (AMS)">Amsterdam (AMS) - Schiphol Airport</option>
        <option value="Madrid (MAD)">Madrid (MAD) - Adolfo Suárez Barajas</option>
        <option value="Barcelona (BCN)">Barcelona (BCN) - El Prat Airport</option>
        <option value="Rome (FCO)">Rome (FCO) - Fiumicino Airport</option>
        <option value="Milan (MXP)">Milan (MXP) - Malpensa Airport</option>
        <option value="Zurich (ZRH)">Zurich (ZRH) - Zurich Airport</option>
        <option value="Geneva (GVA)">Geneva (GVA) - Geneva Airport</option>
        <option value="Vienna (VIE)">Vienna (VIE) - Vienna Intl</option>
        <option value="Brussels (BRU)">Brussels (BRU) - Brussels Airport</option>
        <option value="Copenhagen (CPH)">Copenhagen (CPH) - Copenhagen Airport</option>
        <option value="Dublin (DUB)">Dublin (DUB) - Dublin Airport</option>
        <option value="Istanbul (IST)">Istanbul (IST) - Istanbul Airport</option>

        <!-- North America -->
        <option value="New York (JFK)">New York (JFK) - John F. Kennedy Intl</option>
        <option value="New York (EWR)">New York (EWR) - Newark Liberty Intl</option>
        <option value="Los Angeles (LAX)">Los Angeles (LAX) - Los Angeles Intl</option>
        <option value="San Francisco (SFO)">San Francisco (SFO) - San Francisco Intl</option>
        <option value="Chicago (ORD)">Chicago (ORD) - O'Hare International</option>
        <option value="Dallas (DFW)">Dallas (DFW) - Dallas/Fort Worth Intl</option>
        <option value="Atlanta (ATL)">Atlanta (ATL) - Hartsfield-Jackson Intl</option>
        <option value="Miami (MIA)">Miami (MIA) - Miami International</option>
        <option value="Seattle (SEA)">Seattle (SEA) - Seattle-Tacoma Intl</option>
        <option value="Boston (BOS)">Boston (BOS) - Logan International</option>
        <option value="Toronto (YYZ)">Toronto (YYZ) - Pearson International</option>
        <option value="Vancouver (YVR)">Vancouver (YVR) - Vancouver Intl</option>

        <!-- Asia-Pacific -->
        <option value="Singapore (SIN)">Singapore (SIN) - Changi Airport</option>
        <option value="Bangkok (BKK)">Bangkok (BKK) - Suvarnabhumi Airport</option>
        <option value="Phuket (HKT)">Phuket (HKT) - Phuket Intl</option>
        <option value="Kuala Lumpur (KUL)">Kuala Lumpur (KUL) - KLIA</option>
        <option value="Bali (DPS)">Bali (DPS) - Ngurah Rai Intl</option>
        <option value="Tokyo (HND)">Tokyo (HND) - Haneda Airport</option>
        <option value="Tokyo (NRT)">Tokyo (NRT) - Narita International</option>
        <option value="Seoul (ICN)">Seoul (ICN) - Incheon International</option>
        <option value="Hong Kong (HKG)">Hong Kong (HKG) - Hong Kong Intl</option>
        <option value="Sydney (SYD)">Sydney (SYD) - Kingsford Smith</option>
        <option value="Melbourne (MEL)">Melbourne (MEL) - Melbourne Airport</option>
        <option value="Auckland (AKL)">Auckland (AKL) - Auckland Airport</option>
    </datalist>

    <script>
        async function runDisruptionScenario(delayMinutes) {
            const best = window.t10LastSearch && window.t10LastSearch.best_flight;
            if (!best) return;
            try {
                const response = await fetch('/api/disruption/simulate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ fare: best, delay_minutes: delayMinutes, cancelled: false })
                });
                const data = await response.json();
                const card = document.getElementById('decisionWorkspaceCard');
                if (card && data) {
                    card.classList.remove('hidden');
                    card.innerHTML = '<div class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-2">Disruption scenario</div><pre class="text-xs bg-slate-50 p-3 rounded-lg overflow-auto">' + escapeHtml(JSON.stringify(data, null, 2)) + '</pre>';
                }
            } catch (e) { /* scenario unavailable */ }
        }
    </script>
    <script>
        let currentTab = 'form';

        function defaultTravelDate() {
            const date = new Date();
            date.setDate(date.getDate() + 14);
            return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
        }

        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, char => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            })[char]);
        }

        function safeBookingUrl(raw) {
            const approvedHosts = new Set([
                'www.goindigo.in', 'goindigo.in', 'www.airindia.com', 'airindia.com',
                'www.spicejet.com', 'spicejet.com', 'www.akasaair.com', 'akasaair.com',
                'www.emirates.com', 'emirates.com', 'www.qatarairways.com',
                'qatarairways.com', 'www.britishairways.com', 'britishairways.com',
                'www.google.com', 'google.com'
            ]);
            try {
                const url = new URL(raw);
                return url.protocol === 'https:' && (!url.port || url.port === '443') && approvedHosts.has(url.hostname.toLowerCase())
                    ? url.href
                    : null;
            } catch {
                return null;
            }
        }

        function escapeText(value) {
            return String(value ?? '').replace(/[&<>'"]/g, ch => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            }[ch]));
        }

        function setText(node, value) {
            node.textContent = String(value ?? '');
        }

        function switchTab(tab) {
            currentTab = tab;
            const formBtn = document.getElementById('tabFormBtn');
            const promptBtn = document.getElementById('tabPromptBtn');
            const formContent = document.getElementById('formTabContent');
            const promptContent = document.getElementById('promptTabContent');

            if (tab === 'form') {
                formBtn.className = "text-blue-600 border-b-2 border-blue-600 pb-2 -mb-3 transition";
                promptBtn.className = "text-slate-400 hover:text-slate-600 border-b-2 border-transparent pb-2 -mb-3 transition";
                formContent.classList.remove('hidden');
                promptContent.classList.add('hidden');
            } else {
                promptBtn.className = "text-blue-600 border-b-2 border-blue-600 pb-2 -mb-3 transition";
                formBtn.className = "text-slate-400 hover:text-slate-600 border-b-2 border-transparent pb-2 -mb-3 transition";
                promptContent.classList.remove('hidden');
                formContent.classList.add('hidden');
            }
        }

        function setFormDirect(orig, dest, date, budget) {
            document.getElementById('formOrigin').value = orig;
            document.getElementById('formDest').value = dest;
            document.getElementById('formDate').value = date;
            document.getElementById('formBudget').value = budget;
            switchTab('form');
            runSearch();
        }

        function formatMoney(value, currency) {
            const symbol = { INR: '₹', USD: '$', EUR: '€', GBP: '£', AED: 'AED ', SGD: 'S$', AUD: 'A$', CAD: 'C$' }[currency] || `${currency} `;
            return `${symbol}${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
        }

        function sleep(ms) {
            return new Promise(resolve => setTimeout(resolve, ms));
        }

        function addStepperStep(phase, msg, state) {
            const list = document.getElementById('stepperStepsList');
            const item = document.createElement('div');
            item.className = "flex items-start gap-3 p-3 rounded-xl border border-slate-100 bg-slate-50/50 step-transition text-xs";

            const colorClass = phase === 'PLAN' ? 'bg-purple-100 text-purple-700' :
                               phase === 'ACT' ? 'bg-blue-100 text-blue-700' :
                               phase === 'OBSERVE' ? 'bg-emerald-100 text-emerald-700' :
                               phase === 'EVALUATE' ? 'bg-amber-100 text-amber-700' :
                               'bg-indigo-100 text-indigo-700';

            const phaseSpan = document.createElement('span');
            phaseSpan.className = `px-2.5 py-0.5 rounded font-mono font-bold uppercase tracking-wider text-[10px] ${colorClass}`;
            phaseSpan.textContent = phase;

            const textSpan = document.createElement('span');
            textSpan.className = 'text-slate-700 flex-1 leading-relaxed font-medium';
            textSpan.textContent = msg;

            const marker = document.createElement('span');
            marker.className = 'text-[10px] font-bold text-emerald-600 font-mono';
            marker.textContent = '✓';

            item.append(phaseSpan, textSpan, marker);
            list.appendChild(item);
        }

        document.addEventListener('DOMContentLoaded', () => {
            const dateInput = document.getElementById('formDate');
            if (dateInput) {
                const todayStr = new Date().toISOString().split('T')[0];
                if (!dateInput.min || dateInput.min.startsWith('__')) {
                    dateInput.min = todayStr;
                }
                if (!dateInput.value || dateInput.value.startsWith('__')) {
                    dateInput.value = defaultTravelDate();
                }
            }
        });

        async function runSearch() {
            const btnText = document.getElementById('btnText');
            const spinner = document.getElementById('btnSpinner');
            const searchBtn = document.getElementById('searchBtn');
            const stepperContainer = document.getElementById('stepperContainer');
            const stepperList = document.getElementById('stepperStepsList');
            const stepperBadge = document.getElementById('stepperStatusBadge');

            let payload = {};
            if (currentTab === 'form') {
                const orig = document.getElementById('formOrigin').value.trim();
                const dest = document.getElementById('formDest').value.trim();
                const date = document.getElementById('formDate').value.trim();
                const budget = document.getElementById('formBudget').value.trim();
                const currency = document.getElementById('formCurrency').value;
                const pax = document.getElementById('formPax').value;
                const cabin = document.getElementById('formCabin').value;
                const time = document.getElementById('formTime').value;
                const maxStops = document.getElementById('formMaxStops').value;

                const dateInput = document.getElementById('formDate');
                const todayStr = new Date().toISOString().split('T')[0];
                const minAllowed = (dateInput && dateInput.min && !dateInput.min.startsWith('__')) ? dateInput.min : todayStr;

                if (!orig || !dest || !date) {
                    alert('Please enter origin, destination, and departure date.');
                    return;
                }
                if (date < minAllowed) {
                    alert('Please select today or a future date for departure.');
                    return;
                }
                if (budget && (!Number.isFinite(Number(budget)) || Number(budget) <= 0)) {
                    alert('Budget must be a positive number.');
                    return;
                }

                let promptParts = [`Find me a flight from ${orig} to ${dest} on ${date}`];
                if (budget) promptParts.push(`under ${currency === 'INR' ? '₹' : currency === 'USD' ? '$' : currency === 'EUR' ? '€' : currency === 'GBP' ? '£' : currency} ${budget} ${currency}`);
                if (time) promptParts.push(time);
                if (pax > 1) promptParts.push(`for ${pax} passengers`);
                if (cabin !== 'economy') promptParts.push(`in ${cabin}`);

                payload = { 
                    query: promptParts.join(' '),
                    origin: orig,
                    destination: dest,
                    travel_date: date,
                    budget: budget ? parseFloat(budget) : null,
                    currency,
                    passengers: parseInt(pax) || 1,
                    cabin_class: cabin,
                    preferred_departure: time || null,
                    max_stops: maxStops === '' ? null : parseInt(maxStops, 10)
                };
            } else {
                const query = document.getElementById('promptInput').value.trim();
                if (!query) {
                    alert('Please enter a flight query.');
                    return;
                }
                payload = { query };
            }

            btnText.innerText = "Finding Best Option...";
            spinner.classList.remove('hidden');
            searchBtn.disabled = true;
            stepperContainer.classList.remove('hidden');
            stepperList.innerHTML = '';
            stepperBadge.innerText = "EXECUTING";
            stepperBadge.className = "text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 animate-pulse";

            try {
                const resp = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await resp.json();
                if (!resp.ok) {
                    throw new Error(data.error?.message || 'Flight search request failed.');
                }

                const traceSteps = Array.isArray(data.trace) ? data.trace : [];
                stepperList.innerHTML = '';
                if (!traceSteps.length) {
                    addStepperStep('OBSERVE', 'Request completed successfully, but no backend execution trace was returned.');
                } else {
                    traceSteps.forEach(step => {
                        const phase = step && step.phase ? step.phase : 'OBSERVE';
                        const content = step && step.content ? step.content : 'Backend execution status';
                        addStepperStep(phase, content, step.tool_calls ? 'tool' : 'status');
                    });
                }

                stepperBadge.innerText = "COMPLETED";
                stepperBadge.className = "text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200";

                renderResults(data);
            } catch (err) {
                document.getElementById('bestOptionCard').innerHTML = `
                    <div class="p-8 text-center bg-red-50 border border-red-200 rounded-2xl">
                        <div class="inline-flex items-center justify-center p-3 bg-red-100 text-red-600 rounded-full mb-3">
                            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                            </svg>
                        </div>
                        <h3 class="text-base font-bold text-red-800 mb-1">Search Error</h3>
                        <p class="text-xs text-red-600 max-w-md mx-auto">${escapeHtml(err.message || 'Flight search failed.')}</p>
                    </div>
                `;
                stepperBadge.innerText = "ERROR";
                stepperBadge.className = "text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-full bg-red-50 text-red-700 border border-red-200";
            } finally {
                btnText.innerText = "Find Best Option";
                spinner.classList.add('hidden');
                searchBtn.disabled = false;
            }
        }

        function verificationBadge(label, value) {
            const colors = { VERIFIED: 'badge-verified', UNVERIFIED: 'badge-unverified', UNKNOWN: 'badge-unverified', 'NOT CONFIRMED': 'badge-unavailable' };
            return '<span class="inline-flex items-center gap-1 text-[10px] font-bold uppercase px-2 py-0.5 rounded border ' + (colors[value] || 'badge-unverified') + '">' + escapeHtml(label) + ': ' + escapeHtml(value || 'UNKNOWN') + '</span>';
        }

        function renderDecisionWorkspace(data) {
            window.t10LastSearch = data;
            const banner = document.getElementById('providerStatusBanner');
            const ps = data.provider_statuses || {};
            const wsStatus = (data.decision_workspace || {}).source_status || {};
            const statuses = Object.keys(ps).length ? ps : wsStatus;
            const entries = Object.entries(statuses);
            if (entries.length) {
                const failed = entries.filter(([, s]) => ['error', 'malformed_response', 'provider_unavailable', 'unavailable'].includes(s));
                const partial = entries.some(([, s]) => s === 'partial_response');
                banner.className = 'rounded-2xl border p-4 text-xs ' + (failed.length === entries.length ? 'bg-red-50 border-red-200 text-red-800' : failed.length || partial ? 'bg-amber-50 border-amber-200 text-amber-800' : 'bg-emerald-50 border-emerald-200 text-emerald-800');
                banner.innerHTML = '<span class="font-bold uppercase tracking-wider">Provider status: </span>' + entries.map(([k, s]) => escapeHtml(k) + '=' + escapeHtml(s)).join(' · ') +
                    (failed.length === entries.length ? ' — both providers unavailable; results may be absent or unverified.' : failed.length ? ' — partial provider data; missing evidence is labeled UNKNOWN/NOT CONFIRMED.' : '');
                banner.classList.remove('hidden');
            } else {
                banner.classList.add('hidden');
            }
            const card = document.getElementById('decisionWorkspaceCard');
            const best = data.best_flight;
            const ws = data.decision_workspace || {};
            const recs = ws.recommendations && ws.recommendations.categories ? ws.recommendations.categories : {};
            const parts = [];
            parts.push('<div class="flex items-center justify-between mb-3"><h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider">Decision Workspace — Verification, Scoring & Risk</h3><button onclick="saveCurrentTrip()" class="text-[11px] font-semibold text-blue-700 border border-blue-200 rounded-lg px-3 py-1.5 hover:bg-blue-50">Save trip</button></div>');
            if (best && best.verification) {
                parts.push('<div class="flex flex-wrap gap-1.5 mb-3">' +
                    verificationBadge('Fare', best.verification.fare) +
                    verificationBadge('Schedule', best.verification.schedule) +
                    verificationBadge('Seats', best.verification.seats) +
                    verificationBadge('Booking', best.verification.booking) +
                    (best.verification.freshness_status ? verificationBadge('Freshness', best.verification.freshness_status) : '') + '</div>');
                const ts = [];
                if (best.retrieved_at) ts.push('fare checked ' + new Date(best.retrieved_at).toLocaleString());
                if (best.schedule_retrieved_at) ts.push('schedule checked ' + new Date(best.schedule_retrieved_at).toLocaleString());
                if (best.verification && best.verification.last_checked_at) ts.push('verification at ' + new Date(best.verification.last_checked_at).toLocaleString());
                if (ts.length) parts.push('<div class="text-[10px] text-slate-400 mb-3">' + escapeHtml(ts.join(' · ')) + '</div>');
            }
            const bq = ws.best_result_quality;
            if (bq) {
                const rc = bq.readiness_status === 'READY' ? 'text-emerald-700' : bq.readiness_status === 'PARTIALLY_VERIFIED' ? 'text-amber-700' : 'text-red-700';
                parts.push('<div class="mb-3 text-[11px]"><span class="font-bold text-slate-500 uppercase">Result readiness: </span><span class="' + rc + ' font-bold">' + escapeHtml(bq.readiness_status) + '</span>' +
                    (bq.readiness_reasons && bq.readiness_reasons[0] ? ' <span class="text-slate-500">— ' + escapeHtml(bq.readiness_reasons[0]) + '</span>' : '') + '</div>');
            }
            if (best && best.score_breakdown && best.score_breakdown.factors) {
                const sb = best.score_breakdown;
                parts.push('<div class="mb-3"><div class="text-[11px] font-bold text-slate-500 uppercase mb-1">Score ' + escapeHtml(String(sb.score)) + '/100</div><div class="space-y-1">');
                Object.keys(sb.factors).forEach(name => {
                    const f = sb.factors[name];
                    parts.push('<div class="flex items-center gap-2 text-[11px]"><span class="w-32 text-slate-500">' + escapeHtml(name) + '</span><div class="flex-1 h-1.5 bg-slate-100 rounded"><div class="h-1.5 bg-blue-500 rounded" style="width:' + Math.round((f.weight * f.score) * 100) + '%"></div></div><span class="w-24 text-right text-slate-600">' + escapeHtml(String(f.score)) + ' × ' + escapeHtml(String(f.weight)) + '</span></div>');
                });
                if (sb.missing_factors && sb.missing_factors.length) parts.push('<div class="text-[10px] text-slate-400 mt-1">Excluded (missing data): ' + escapeHtml(sb.missing_factors.join(', ')) + '</div>');
                parts.push('</div></div>');
            }
            const tabs = [];
            if (recs.cheapest) tabs.push(['cheapest', recs.cheapest]);
            if (recs.fastest) tabs.push(['fastest', recs.fastest]);
            if (recs.best_overall_value || recs.best_overall) tabs.push(['best', recs.best_overall_value || recs.best_overall]);
            if (tabs.length) {
                parts.push('<div class="mb-3"><div class="text-[11px] font-bold text-slate-500 uppercase mb-1">Category winners</div><div class="flex gap-2">');
                tabs.forEach(([key, cat]) => {
                    const f = cat.flight || {};
                    parts.push('<div class="flex-1 border border-slate-200 rounded-lg p-2.5 text-[11px]"><div class="font-bold text-slate-700 uppercase">' + escapeHtml(key) + '</div><div class="font-semibold text-slate-900 mt-0.5">' + escapeHtml(f.flight_number || cat.label || 'INSUFFICIENT DATA') + '</div><div class="text-slate-500">' + escapeHtml(String(cat.value != null ? cat.value : (cat.explanation || ''))) + '</div></div>');
                });
                parts.push('</div></div>');
            }
            const risks = ws.connection_risk || {};
            const riskEntries = Object.entries(risks);
            if (riskEntries.length) {
                parts.push('<div class="mb-3"><div class="text-[11px] font-bold text-slate-500 uppercase mb-1">Connection risk</div><div class="space-y-1">');
                riskEntries.forEach(([fn, r]) => {
                    const lvl = r.risk_level || 'UNKNOWN';
                    const cls = lvl === 'HIGH' ? 'text-red-700' : lvl === 'MEDIUM' ? 'text-amber-700' : lvl === 'LOW' ? 'text-emerald-700' : 'text-slate-500';
                    parts.push('<div class="text-[11px]"><span class="font-semibold">' + escapeHtml(fn) + '</span> <span class="' + cls + ' font-bold">' + escapeHtml(lvl) + '</span> <span class="text-slate-400">' + escapeHtml(r.ticket_structure || '') + '</span></div>');
                });
                parts.push('</div></div>');
            }
            const cost = ws.cost;
            if (cost) {
                parts.push('<div class="text-[11px]"><span class="font-bold text-slate-500 uppercase">Known fare: </span><span class="text-slate-700">' + escapeHtml(cost.known_cost != null ? formatMoney(cost.known_cost, cost.currency || 'INR') : 'unknown') + '</span> <span class="text-slate-400">(' + escapeHtml((cost.unknown_expenses || []).join(', ') || 'no unknowns') + ' unknown)</span></div>');
            }
            const disruption = ws.disruption_scenarios || {};
            const delayKeys = Object.keys(disruption);
            if (delayKeys.length) {
                parts.push('<div class="mt-3 pt-3 border-t border-slate-100"><div class="text-[11px] font-bold text-slate-500 uppercase mb-1">Disruption what-if</div><div class="flex flex-wrap gap-1.5">');
                delayKeys.forEach(d => {
                    parts.push('<button onclick="runDisruptionScenario(' + escapeHtml(d) + ')" class="text-[10px] font-semibold px-2 py-1 rounded border border-slate-200 hover:bg-slate-50 text-slate-600">' + escapeHtml(d + ' min delay') + '</button>');
                });
                parts.push('</div></div>');
            }
            card.innerHTML = parts.join('');
            card.classList.toggle('hidden', !best && !tabs.length);
        }

        async function saveCurrentTrip() {
            const data = window.t10LastSearch;
            if (!data || !data.best_flight) { alert('Run a search first.'); return; }
            try {
                const result = await fetch('/api/trips/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        title: (data.best_flight.origin || '') + ' to ' + (data.best_flight.destination || '') + ' - ' + (data.best_flight.travel_date || ''),
                        payload: { search_request: data.search_request, selected_itinerary: data.best_flight, decision_workspace: data.decision_workspace, request_id: data.request_id, searched_at: data.searched_at }
                    })
                });
                const saved = await result.json();
                trackEvent('trip_saved', data.best_flight.flight_number);
                alert(saved.trip_id ? 'Trip saved.' : 'Save failed.');
                loadSavedTrips();
            } catch (e) { alert('Save failed.'); }
        }

        async function loadSavedTrips() {
            try {
                const response = await fetch('/api/trips?limit=10');
                const trips = await response.json();
                const target = document.getElementById('savedTrips');
                if (!target || !Array.isArray(trips)) return;
                target.innerHTML = '<div class="text-[11px] font-bold text-slate-500 uppercase mb-1">Saved trips</div>';
                trips.forEach(trip => {
                    const row = document.createElement('div');
                    row.className = 'text-[11px] text-slate-600 py-1 border-t border-slate-100';
                    row.textContent = trip.title + ' · ' + new Date(trip.created_at).toLocaleString() + ' · ' + (trip.original_evidence_status || 'UNKNOWN');
                    target.appendChild(row);
                });
            } catch (e) { /* history unavailable */ }
        }
        document.addEventListener('DOMContentLoaded', loadSavedTrips);

        function renderResults(data) {
            renderDecisionWorkspace(data);
            const best = data.best_flight;
            const req = data.search_request || {};
            const isSelfTransfer = best?.itinerary_kind === 'connecting_self_transfer';
            const freshnessText = (timestamp, thresholdMinutes = 15) => {
                if (!timestamp) return 'UNKNOWN';
                const retrieved = new Date(timestamp);
                if (!Number.isFinite(retrieved.getTime())) return 'UNKNOWN';
                const ageMinutes = Math.max(0, (Date.now() - retrieved.getTime()) / 60000);
                const state = ageMinutes <= thresholdMinutes ? 'FRESH' : ageMinutes <= thresholdMinutes * 4 ? 'AGING' : 'STALE';
                return `${state} · ${retrieved.toLocaleString()}`;
            };
            const card = document.getElementById('bestOptionCard');
            const suggCard = document.getElementById('suggestionsCard');
            const suggList = document.getElementById('suggestionsList');

            // Render Best Option Card (Exact Match from User's Screenshot)
            if (best && best.price) {
                const bookingUrl = best.booking_url && safeBookingUrl(best.booking_url) ? safeBookingUrl(best.booking_url) : null;
                const selectionReason = data.recommendation_reason || 'Optimal verified flight within budget.';
                const textParts = [
                    '<div class="bg-gradient-to-r from-blue-600 to-indigo-700 p-6 text-white">',
                    '<div class="flex items-center justify-between">',
                    '<span class="text-xs uppercase tracking-widest font-semibold bg-white/20 px-3 py-1 rounded-full">✈️ ' + escapeHtml(best.schedule_matched ? 'TOP VERIFIED FLIGHT' : best.price_status === 'verified' ? 'TOP FARE OPTION' : 'TOP FLIGHT OPTION') + '</span>',
                    '<span class="text-xs font-medium px-2.5 py-0.5 rounded-md ' + (best.price_status === 'verified' && !isSelfTransfer ? 'badge-verified' : best.price_status === 'unavailable' || best.price_status === 'error' ? 'badge-unavailable' : 'badge-unverified') + '">' + escapeHtml(isSelfTransfer ? 'SELF-TRANSFER ESTIMATE' : `${best.price_status.toUpperCase()} FARE`) + '</span>',
                    '</div>',
                    '<div class="mt-4 flex flex-col sm:flex-row sm:items-baseline justify-between gap-2">',
                    '<div>',
                    '<h2 class="text-3xl font-extrabold tracking-tight">' + escapeHtml((best.airline || '') + ' ' + best.flight_number) + '</h2>',
                    '<p class="text-blue-100 text-sm mt-0.5 font-medium">' + escapeHtml((req.origin_city || best.origin) + ' (' + best.origin + ') → ' + (req.destination_city || best.destination) + ' (' + best.destination + ')') + '</p>',
                    '</div>',
                    '<div class="sm:text-right">',
                    '<div class="text-3xl font-extrabold">' + escapeHtml(formatMoney(best.price, best.currency || req.currency || 'INR')) + '</div>',
                    '<div class="text-xs text-blue-200 mt-0.5">' + escapeHtml(req.budget ? (best.price <= req.budget ? '✓ Within Budget (' + formatMoney(req.budget, req.currency || 'INR') + ')' : 'Exceeds Budget') : 'No budget limit') + '</div>',
                    '</div>',
                    '</div>',
                    '</div>'
                ];
                if (isSelfTransfer) {
                    textParts.push('<div class="px-6 py-4 bg-amber-50 border-b border-amber-100 text-xs text-amber-900">');
                    textParts.push('<p class="font-semibold">Estimated total for separate bookings — not a confirmed through-ticket.</p>');
                    textParts.push('<p class="mt-1">' + escapeHtml(best.combined_price_note || 'Each flight leg must be booked separately. The combined itinerary and total are not confirmed as one booking.') + '</p>');
                    textParts.push('<div class="mt-3 space-y-2">');
                    (best.legs || []).forEach((leg, index) => {
                        textParts.push('<div class="flex flex-wrap gap-x-3">');
                        textParts.push('<span class="font-semibold">Leg ' + index + 1 + ': ' + escapeHtml(leg.flight_number) + '</span>');
                        textParts.push('<span>' + escapeHtml(leg.origin) + ' → ' + escapeHtml(leg.destination) + '</span>');
                        textParts.push('<span>' + escapeHtml(leg.departure_time || 'N/A') + ' – ' + escapeHtml(leg.arrival_time || 'N/A') + '</span>');
                        textParts.push('<span>' + escapeHtml(leg.price != null ? formatMoney(leg.price, leg.currency || req.currency || 'INR') : 'Price unavailable') + '</span>');
                        textParts.push('<span class="font-semibold uppercase">' + escapeHtml(leg.price_status) + ' fare</span>');
                        textParts.push('</div>');
                    });
                    textParts.push('</div></div>');
                }
                textParts.push('<div class="p-6 grid grid-cols-2 sm:grid-cols-3 gap-4 text-xs border-b border-slate-100 bg-slate-50/50">');
                textParts.push('<div><span class="text-slate-400 block font-medium">DATE</span><span class="font-semibold text-slate-800">' + escapeHtml(best.travel_date) + '</span></div>');
                textParts.push('<div><span class="text-slate-400 block font-medium">DEPARTURE</span><span class="font-semibold text-slate-800">' + escapeHtml(best.departure_time || 'N/A') + '</span></div>');
                textParts.push('<div><span class="text-slate-400 block font-medium">ARRIVAL</span><span class="font-semibold text-slate-800">' + escapeHtml(best.arrival_time || 'N/A') + '</span></div>');
                textParts.push('<div><span class="text-slate-400 block font-medium">SCHEDULE MATCH</span><span class="font-semibold ' + (best.schedule_matched ? 'text-emerald-700' : 'text-amber-700') + '">' + escapeHtml(best.schedule_matched ? 'MATCHED' : 'FARE ONLY') + '</span></div>');
                textParts.push('<div><span class="text-slate-400 block font-medium">SEAT AVAILABILITY</span><span class="font-semibold text-slate-700">NOT CONFIRMED</span></div>');
                textParts.push('<div><span class="text-slate-400 block font-medium">FARE FRESHNESS</span><span class="font-semibold text-slate-700">' + escapeHtml(freshnessText(best.retrieved_at)) + '</span></div>');
                textParts.push('<div><span class="text-slate-400 block font-medium">WEB VERIFICATION</span><span class="font-semibold text-blue-700 uppercase">' + escapeHtml(best.web_verification_status || 'UNAVAILABLE') + '</span></div>');
                textParts.push('</div><div class="p-6 flex flex-col sm:flex-row items-center justify-between gap-4"><div class="text-xs text-slate-600"><span class="font-semibold text-slate-800 block mb-0.5">Selection Rationale:</span>' + escapeHtml(selectionReason) + '</div>');
                if (bookingUrl && !isSelfTransfer) {
                    textParts.push('<a href="' + escapeHtml(bookingUrl) + '" target="_blank" rel="noopener noreferrer" data-et="booking_link_clicked" data-fn="' + escapeHtml(best.flight_number || '') + '" onclick="trackEvent(this.dataset.et, this.dataset.fn)" class="w-full sm:w-auto px-6 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl text-xs shadow transition text-center whitespace-nowrap">Search ' + escapeHtml(best.booking_provider || 'Airline') + ' →</a>');
                } else {
                    textParts.push('<span class="text-xs text-slate-500">' + escapeHtml(isSelfTransfer ? 'No single booking link — book each leg separately.' : 'Booking link unavailable') + '</span>');
                }
                textParts.push('</div>');
                card.innerHTML = textParts.join('');
                suggCard.classList.add('hidden');
            } else {
                const noResults = document.createElement('div');
                noResults.className = 'p-8 text-center bg-slate-50';
                noResults.innerHTML = '<div class="inline-flex items-center justify-center p-3 bg-amber-100 text-amber-600 rounded-full mb-3"><svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg></div><h3 class="text-base font-bold text-slate-800 mb-1">No Flights Available Matching Exact Criteria</h3><p class="text-xs text-slate-500 max-w-md mx-auto">' + escapeHtml(data.recommendation_reason || 'No flights found within the requested budget and route constraints.') + '</p>';
                card.replaceChildren(noResults);

                if (data.suggestions && data.suggestions.length > 0) {
                    suggList.replaceChildren(...data.suggestions.map(s => {
                        const item = document.createElement('li');
                        item.className = 'flex items-start gap-2';
                        const bullet = document.createElement('span');
                        bullet.className = 'text-amber-500 font-bold';
                        bullet.textContent = '•';
                        const text = document.createElement('span');
                        text.textContent = s;
                        item.append(bullet, text);
                        return item;
                    }));
                    suggCard.classList.remove('hidden');
                } else {
                    suggCard.classList.add('hidden');
                }
            }

            const bd = document.getElementById('requestBreakdown');
            bd.replaceChildren(
                ...[
                    ['Origin', req.origin_city || '-', req.origin_code || '-'],
                    ['Destination', req.destination_city || '-', req.destination_code || '-'],
                    ['Travel Date', req.travel_date || '-', '-'],
                    ['Budget', req.budget ? formatMoney(req.budget, req.currency || 'INR') : 'None', '-']
                ].map(([label, value, code]) => {
                    const box = document.createElement('div');
                    box.className = 'p-3 bg-slate-50 rounded-xl border border-slate-100';
                    const caption = document.createElement('span');
                    caption.className = 'text-xs text-slate-400 block font-medium';
                    caption.textContent = label;
                    const boxValue = document.createElement('span');
                    boxValue.className = 'font-bold text-slate-800';
                    boxValue.textContent = label === 'Budget' ? value : `${value} (${code})`;
                    box.append(caption, boxValue);
                    return box;
                })
            );

            const traceBox = document.getElementById('traceSteps');
            traceBox.replaceChildren(...(data.trace || []).map(t => {
                const row = document.createElement('div');
                row.className = 'flex items-start gap-3 text-xs p-2.5 bg-slate-50 rounded-xl border border-slate-100';
                const badge = document.createElement('span');
                badge.className = 'px-2 py-0.5 rounded font-mono font-bold uppercase tracking-wider text-[10px] ' + (t.phase === 'PLAN' ? 'bg-purple-100 text-purple-700' : t.phase === 'ACT' ? 'bg-blue-100 text-blue-700' : t.phase === 'OBSERVE' ? 'bg-emerald-100 text-emerald-700' : t.phase === 'EVALUATE' ? 'bg-amber-100 text-amber-700' : 'bg-indigo-100 text-indigo-700');
                badge.textContent = t.phase;
                const text = document.createElement('span');
                text.className = 'text-slate-700 flex-1 leading-relaxed';
                text.textContent = t.content;
                row.append(badge, text);
                return row;
            }));

            window.t10Candidates = data.matched_fares || data.fares_found || [];
            window.t10Shortlist = window.t10Shortlist || new Set();
            renderCandidateTable();
        }

        function fareKey(f) { return (f.ignav_id || '') + '|' + f.flight_number + '|' + (f.departure_at || f.departure_time || ''); }

        function farePrice(f) {
            if (f.comparison_amount != null) { const v = parseFloat(f.comparison_amount); if (isFinite(v)) return v; }
            return f.price != null ? f.price : Infinity;
        }

        function fareDuration(f) {
            if (!f.departure_at || !f.arrival_at) return Infinity;
            const d = new Date(f.departure_at), a = new Date(f.arrival_at);
            if (!isFinite(d) || !isFinite(a)) return Infinity;
            return (a - d) / 60000;
        }

        function renderCandidateTable() {
            const tb = document.getElementById('candidatesTableBody');
            const sortMode = (document.getElementById('sortSelect') || {}).value || 'score';
            const maxStops = (document.getElementById('filterStops') || {}).value;
            const airlineFilter = ((document.getElementById('filterAirline') || {}).value || '').toLowerCase();
            const budgetFilter = parseFloat((document.getElementById('filterBudget') || {}).value);
            let items = (window.t10Candidates || []).slice();
            if (maxStops !== '') items = items.filter(f => (f.stops || 0) <= parseInt(maxStops));
            if (airlineFilter) items = items.filter(f => (f.airline || '').toLowerCase().includes(airlineFilter) || (f.airline_code || '').toLowerCase().includes(airlineFilter));
            const cabinFilter = (document.getElementById('filterCabin') || {}).value;
            if (cabinFilter) items = items.filter(f => !f.cabin_class || f.cabin_class.toLowerCase() === cabinFilter);
            if (isFinite(budgetFilter)) items = items.filter(f => farePrice(f) <= budgetFilter);
            const sorters = {
                score: (a, b) => {
                    const sa = (a.score_breakdown || {}).score || 0, sb = (b.score_breakdown || {}).score || 0;
                    if (sb !== sa) return sb - sa;
                    const fa = ((a.score_breakdown || {}).factors || {}).verification, fb = ((b.score_breakdown || {}).factors || {}).verification;
                    const va = fa ? (fa.score || 0) : 0, vb = fb ? (fb.score || 0) : 0;
                    if (vb !== va) return vb - va;
                    const pa = farePrice(a), pb = farePrice(b);
                    if (pa !== pb) return pa - pb;
                    if ((a.stops || 0) !== (b.stops || 0)) return (a.stops || 0) - (b.stops || 0);
                    const da = String(a.departure_at || a.departure_time || 'zzz'), db = String(b.departure_at || b.departure_time || 'zzz');
                    if (da !== db) return da < db ? -1 : 1;
                    return String(a.flight_number || '').localeCompare(String(b.flight_number || ''));
                },
                price: (a, b) => farePrice(a) - farePrice(b),
                departure: (a, b) => String(a.departure_at || a.departure_time || 'zzz') < String(b.departure_at || b.departure_time || 'zzz') ? -1 : 1,
                duration: (a, b) => fareDuration(a) - fareDuration(b),
                stops: (a, b) => (a.stops || 0) - (b.stops || 0) || farePrice(a) - farePrice(b),
            };
            items.sort(sorters[sortMode] || sorters.score);
            if (items.length > 0) {
                tb.replaceChildren(...items.slice(0, 25).map(f => {
                    const row = document.createElement('tr');
                    row.className = 'hover:bg-slate-50 transition';
                    const key = fareKey(f);
                    const starred = window.t10Shortlist.has(key);
                    const score = (f.score_breakdown || {}).score;
                    const verify = f.verification ? [f.verification.fare, f.verification.schedule].join('/') : (f.trust_status && f.trust_status.overall) || 'UNKNOWN';
                    const freshness = f.verification && f.verification.freshness_status ? f.verification.freshness_status : '';
                    const cells = [
                        '<button data-key="' + escapeHtml(key) + '" onclick="toggleShortlist(this.dataset.key, this)" class="text-sm ' + (starred ? 'text-amber-500' : 'text-slate-300') + '">' + (starred ? '★' : '☆') + '</button>',
                        f.flight_number,
                        f.airline || '-',
                        `${f.origin} → ${f.destination}`,
                        `${f.departure_time || '-'} - ${f.arrival_time || '-'}`,
                        f.price ? formatMoney(f.price, f.currency || 'INR') + (f.comparison_amount && f.fx_status === 'converted' ? ' <span class="text-slate-400">≈' + escapeHtml(String(f.comparison_amount)) + ' ' + escapeHtml(f.comparison_currency || '') + '</span>' : '') : 'Unavailable',
                        score != null ? score + '/100' : '-',
                        f.itinerary_kind === 'connecting_self_transfer' ? 'ESTIMATE' : f.price_status,
                        f.schedule_matched ? '✓ MATCH' : f.price_available ? 'FARE ONLY' : '✕ NO FARE',
                        verify + (freshness ? ' · ' + freshness : ''),
                    ];
                    cells.forEach((value, idx) => {
                        const cell = document.createElement('td');
                        cell.className = idx === 0 ? 'px-2 py-3' : idx === 1 ? 'px-4 py-3 font-semibold text-slate-800' : idx === 2 ? 'px-4 py-3 font-sans' : idx === 5 ? 'px-4 py-3 font-semibold text-slate-900' : idx === 6 ? 'px-4 py-3 font-semibold text-blue-700' : idx === 7 ? 'px-4 py-3' : idx === 8 ? 'px-4 py-3 font-semibold ' + (f.schedule_matched ? 'text-emerald-600' : f.price_available ? 'text-blue-600' : 'text-slate-400') : idx === 9 ? 'px-4 py-3 text-[10px] text-slate-500' : 'px-4 py-3';
                        if (idx === 0) { cell.innerHTML = value; }
                        else if (idx === 7) {
                            const badge = document.createElement('span');
                            badge.className = 'px-2 py-0.5 rounded text-[10px] font-semibold uppercase ' + (f.price_status === 'verified' && f.itinerary_kind !== 'connecting_self_transfer' ? 'badge-verified' : f.price_status === 'unavailable' || f.price_status === 'error' ? 'badge-unavailable' : 'badge-unverified');
                            badge.title = f.itinerary_kind === 'connecting_self_transfer' ? 'Estimated sum of separate leg fares; no through-ticket is confirmed.' : '';
                            badge.textContent = value;
                            cell.appendChild(badge);
                        } else {
                            cell.innerHTML = value;
                        }
                        row.appendChild(cell);
                    });
                    return row;
                }));
            } else {
                tb.innerHTML = '<tr><td colspan="10" class="px-4 py-6 text-center text-slate-400 font-sans italic">No candidate fares match the current filters.</td></tr>';
            }
        }

        function metricCard(label, m, fmt) {
            if (!m || m.availability === 'unavailable' || m.value == null) {
                return '<div class="bg-slate-50 rounded-xl p-3 border border-slate-200"><div class="text-[10px] uppercase font-bold text-slate-400">' + escapeHtml(label) + '</div><div class="text-slate-400 text-xs mt-1">No data</div></div>';
            }
            const v = fmt ? fmt(m.value) : (typeof m.value === 'object' ? JSON.stringify(m.value) : m.value);
            return '<div class="bg-slate-50 rounded-xl p-3 border border-slate-200"><div class="text-[10px] uppercase font-bold text-slate-400">' + escapeHtml(label) + '</div><div class="text-slate-800 font-bold text-sm mt-1">' + escapeHtml(String(v)) + (m.unit && typeof m.unit === 'string' && m.unit.length < 12 ? ' <span class="text-[10px] font-normal text-slate-400">' + escapeHtml(m.unit) + '</span>' : '') + '</div><div class="text-[10px] text-slate-400 mt-0.5">n=' + (m.sample_size || 0) + '</div></div>';
        }

        async function loadAnalytics() {
            const body = document.getElementById('analyticsBody');
            try {
                const r = await fetch('/api/analytics/insights');
                const d = await r.json();
                const sec = (title, inner) => '<div class="mt-5"><h3 class="text-xs font-bold uppercase tracking-wider text-slate-500 mb-2">' + title + '</h3><div class="grid grid-cols-2 sm:grid-cols-4 gap-2">' + inner + '</div></div>';
                const money = c => v => v == null ? '—' : (c ? c + ' ' : '') + Number(v).toLocaleString();
                let html = '';
                // Traveller intelligence
                const f = d.fares || {};
                if (f.status === 'ok') {
                    const cur = (f.metrics.currency && f.metrics.currency.value) || '';
                    html += sec('Fare Intelligence',
                        metricCard('Cheapest', f.metrics.cheapest, money(cur)) +
                        metricCard('Average', f.metrics.average, money(cur)) +
                        metricCard('Median', f.metrics.median, money(cur)) +
                        metricCard('Eligible fares', f.metrics.eligible_count) +
                        metricCard('Within budget', f.metrics.within_budget) +
                        metricCard('Outside budget', f.metrics.outside_budget));
                } else html += sec('Fare Intelligence', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(f.reason || 'No data') + '</div>');
                const j = d.journey || {};
                if (j.status === 'ok') {
                    html += sec('Journey Efficiency',
                        metricCard('Shortest', j.metrics.shortest_minutes, v => Math.round(v) + ' min') +
                        metricCard('Average', j.metrics.average_minutes, v => Math.round(v) + ' min') +
                        metricCard('Direct', j.metrics.direct_count) +
                        metricCard('Connecting', j.metrics.connecting_count));
                    if (j.excluded && (j.excluded.naive_or_invalid || j.excluded.missing)) {
                        html += '<div class="text-[10px] text-slate-400 mt-1">Excluded from duration: ' + (j.excluded.naive_or_invalid || 0) + ' naive/invalid, ' + (j.excluded.missing || 0) + ' missing timestamps.</div>';
                    }
                } else html += sec('Journey Efficiency', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(j.reason || 'No data') + '</div>');
                const rec = d.recommendations || {};
                if (rec.status === 'ok') {
                    html += sec('Recommendation Insights',
                        metricCard('Best scores', rec.metrics.best_overall_scores) +
                        metricCard('Cheapest fares', rec.metrics.cheapest_fares) +
                        metricCard('Cheapest→best Δ', rec.metrics.cheapest_vs_best_delta));
                } else html += sec('Recommendation Insights', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(rec.reason || 'No data') + '</div>');
                const rk = d.risks || {};
                if (rk.status === 'ok') {
                    const dist = (rk.metrics.risk_distribution && rk.metrics.risk_distribution.value) || {};
                    html += sec('Travel Risk',
                        metricCard('LOW', {value: dist.LOW || 0, sample_size: rk.sample_size, availability: 'available'}) +
                        metricCard('MEDIUM', {value: dist.MEDIUM || 0, sample_size: rk.sample_size, availability: 'available'}) +
                        metricCard('HIGH', {value: dist.HIGH || 0, sample_size: rk.sample_size, availability: 'available'}) +
                        metricCard('UNKNOWN', {value: dist.UNKNOWN || 0, sample_size: rk.sample_size, availability: 'available'}) +
                        metricCard('NOT_APPLICABLE', {value: dist.NOT_APPLICABLE || 0, sample_size: rk.sample_size, availability: 'available'}) +
                        metricCard('Self-transfers', rk.metrics.self_transfer_count));
                } else html += sec('Travel Risk', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(rk.reason || 'No data') + '</div>');
                // System intelligence
                const p = d.providers || {};
                if (p.status === 'ok') {
                    let cells = '';
                    for (const [name, m] of Object.entries(p.providers || {})) {
                        cells += metricCard(name, {value: m.success_rate_pct != null ? m.success_rate_pct + '%' : null, unit: 'success', sample_size: m.total_requests, availability: m.total_requests ? 'available' : 'unavailable'}) +
                                 metricCard(name + ' avg ms', {value: m.avg_latency_ms, unit: 'ms', sample_size: m.total_requests, availability: m.avg_latency_ms != null ? 'available' : 'unavailable'}) +
                                 metricCard(name + ' failures', {value: m.failed_requests, unit: 'req', sample_size: m.total_requests, availability: 'available'});
                    }
                    html += sec('Provider Performance', cells);
                } else html += sec('Provider Performance', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(p.reason || 'Not enough telemetry') + '</div>');
                const m = d.matching || {};
                if (m.status === 'ok') {
                    const fun = m.funnel || {};
                    html += sec('Matching',
                        metricCard('Searches', {value: fun.searches, sample_size: fun.searches, availability: 'available'}) +
                        metricCard('Fare records', {value: fun.fare_records, sample_size: fun.searches, availability: 'available'}) +
                        metricCard('Eligible', {value: fun.eligible_candidates, sample_size: fun.searches, availability: 'available'}) +
                        metricCard('Fare-only', {value: fun.fare_only_candidates, sample_size: fun.searches, availability: 'available'}));
                } else html += sec('Matching', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(m.reason || 'No data') + '</div>');
                const v = d.verification || {};
                if (v.status === 'ok') {
                    const facets = (v.metrics.facet_distribution || {});
                    let cells = metricCard('Total fares', {value: v.sample_size, sample_size: v.sample_size, availability: 'available'});
                    for (const [facet, dist] of Object.entries(facets)) {
                        cells += metricCard(facet + ' VERIFIED', {value: (dist.VERIFIED || 0), sample_size: v.sample_size, availability: 'available'}) +
                                 metricCard(facet + ' UNKNOWN', {value: (dist.UNKNOWN || 0) + (dist['NOT CONFIRMED'] || 0), sample_size: v.sample_size, availability: 'available'});
                    }
                    html += sec('Verification Quality', cells);
                } else html += sec('Verification Quality', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(v.reason || 'No data') + '</div>');
                const de = d.decision_engine || {};
                if (de.status === 'ok') {
                    const mo = de.metrics || {};
                    html += sec('Decision Engine',
                        metricCard('Searches', {value: mo.total_searches, sample_size: mo.total_searches, availability: 'available'}) +
                        metricCard('With best flight', {value: mo.searches_with_best_flight, sample_size: mo.total_searches, availability: 'available'}) +
                        metricCard('Avg eligible/search', {value: mo.avg_eligible_per_search, sample_size: mo.total_searches, availability: 'available'}) +
                        metricCard('Results found', {value: (mo.search_outcomes || {}).result_found || 0, sample_size: mo.total_searches, availability: 'available'}));
                } else html += sec('Decision Engine', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(de.reason || 'No data') + '</div>');
                const ia = d.interactions || {};
                if (ia.status === 'ok') {
                    const ec = (ia.metrics.event_counts || {});
                    html += sec('Interactions',
                        metricCard('Booking clicks', {value: ec.booking_link_clicked || 0, sample_size: ia.sample_size, availability: 'available'}, v => v + ' (clicks, not bookings)') +
                        metricCard('Shortlist adds', {value: ec.shortlist_added || 0, sample_size: ia.sample_size, availability: 'available'}) +
                        metricCard('Comparisons', {value: ec.comparison_initiated || 0, sample_size: ia.sample_size, availability: 'available'}) +
                        metricCard('Trips saved', {value: ec.trip_saved || 0, sample_size: ia.sample_size, availability: 'available'}));
                } else html += sec('Interactions', '<div class="col-span-4 text-xs text-slate-400">' + escapeHtml(ia.reason || 'No interactions yet') + '</div>');
                body.innerHTML = html;
            } catch (e) {
                body.innerHTML = '<div class="text-xs text-red-500">Analytics unavailable: ' + escapeHtml(String(e)) + '</div>';
            }
        }
        document.addEventListener('DOMContentLoaded', loadAnalytics);

        function trackEvent(eventType, flightKey) {
            try {
                fetch('/api/interactions', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({event_type: eventType, flight_key: flightKey || null})}).catch(() => {});
            } catch (e) {}
        }

        function toggleShortlist(key, btn) {
            const added = !window.t10Shortlist.has(key);
            if (added) window.t10Shortlist.add(key); else window.t10Shortlist.delete(key);
            trackEvent(added ? 'shortlist_added' : 'shortlist_removed', key);
            renderCandidateTable();
        }

        function compareShortlist() {
            const panel = document.getElementById('comparePanel');
            const items = (window.t10Candidates || []).filter(f => window.t10Shortlist.has(fareKey(f)));
            if (!items.length) { panel.classList.add('hidden'); return; }
            trackEvent('comparison_initiated', items.map(fareKey).join(','));
            const fields = [
                ['Flight', f => f.flight_number], ['Airline', f => f.airline || '-'],
                ['Route', f => `${f.origin} → ${f.destination}`],
                ['Departure', f => f.departure_time || '-'], ['Arrival', f => f.arrival_time || '-'],
                ['Stops', f => f.stops ?? '-'],
                ['Native fare', f => f.price != null ? formatMoney(f.price, f.currency || 'INR') : 'Unavailable'],
                ['Compare fare', f => f.comparison_amount ? `${f.comparison_amount} ${f.comparison_currency || ''}` : '-'],
                ['Score', f => (f.score_breakdown || {}).score != null ? (f.score_breakdown.score + '/100') : '-'],
                ['Verification', f => f.verification ? `${f.verification.fare}/${f.verification.schedule}/${f.verification.seats}/${f.verification.booking}` : 'UNKNOWN'],
                ['Freshness', f => f.verification ? f.verification.freshness_status : 'UNKNOWN'],
                ['Connection risk', f => {
                    const risk = (window.t10LastSearch?.decision_workspace?.connection_risk || {})[f.flight_number];
                    return risk ? (risk.risk_level || 'UNKNOWN') : '-';
                }],
                ['Readiness', f => {
                    const q = (window.t10LastSearch?.decision_workspace?.result_quality || {})[f.flight_number];
                    return q ? q.readiness_status : '-';
                }],
            ];
            let html = '<h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-3">Shortlist comparison (' + items.length + ')</h3><div class="overflow-x-auto"><table class="w-full text-left text-[11px]"><thead><tr class="border-b border-slate-200"><th class="px-3 py-2"></th>';
            items.forEach(f => { html += '<th class="px-3 py-2 font-semibold text-slate-800">' + escapeHtml(f.flight_number) + '</th>'; });
            html += '</tr></thead><tbody>';
            fields.forEach(([label, fn]) => {
                html += '<tr class="border-b border-slate-100"><td class="px-3 py-2 font-semibold text-slate-500">' + escapeHtml(label) + '</td>';
                items.forEach(f => { html += '<td class="px-3 py-2">' + escapeHtml(String(fn(f) ?? '-')) + '</td>'; });
                html += '</tr>';
            });
            html += '</tbody></table></div>';
            panel.innerHTML = html;
            panel.classList.remove('hidden');
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve live web user interface."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_date = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")
    html = HTML_TEMPLATE.replace("__TODAY__", today).replace("__DEFAULT_DATE__", default_date)
    return HTMLResponse(content=html)


@app.post(
    "/api/search",
    response_model=FlightSearchResult,
    responses={503: {"model": APIErrorResponse}},
)
def api_search(payload: SearchPayload, request: Request, response: Response):
    """Validate and run a flight search within the browser's isolated session."""
    started_at = perf_counter()
    session_id = request.cookies.get("t10_session", "")
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        session_id = uuid.uuid4().hex
    response.set_cookie(
        "t10_session",
        session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=30 * 24 * 60 * 60,
        path="/",
    )

    if payload.origin and payload.destination and payload.travel_date:
        query = f"Find a flight from {payload.origin} to {payload.destination} on {payload.travel_date}"
        if payload.budget is not None:
            query += f" under {payload.budget:g} {payload.currency}"
        if payload.passengers > 1:
            query += f" for {payload.passengers} passengers"
        if payload.cabin_class != "economy":
            query += f" in {payload.cabin_class}"
        if payload.preferred_departure:
            query += f" {payload.preferred_departure}"
    else:
        query = (payload.query or "").strip()

    request_id = request.state.request_id
    request_agent = FlightOptionFinderAgent(memory=session_memory_store.get(session_id))
    try:
        overrides = None
        if payload.origin and payload.destination and payload.travel_date:
            overrides = {
                "budget": payload.budget,
                "currency": payload.currency,
                "passengers": payload.passengers,
                "cabin_class": payload.cabin_class,
                "preferred_departure": payload.preferred_departure,
                "max_stops": payload.max_stops,
            }
        result = request_agent.run(query, request_id=request_id, overrides=overrides)
    except Exception:
        logger.exception("Flight search failed", extra={"request_id": request_id})
        return JSONResponse(
            status_code=503,
            content={"error": {
                "code": "search_unavailable",
                "message": "Flight search is temporarily unavailable.",
                "request_id": request_id,
            }},
            headers={"X-Request-ID": request_id},
        )

    duration_ms = round((perf_counter() - started_at) * 1000)
    try:
        result.decision_workspace = build_decision_workspace(result)
    except Exception:
        logger.exception("Could not build decision workspace", extra={"request_id": request_id})
        result.decision_workspace = {"error": "Decision analysis is temporarily unavailable."}
    try:
        analytics_store.record_search(result, duration_ms)
    except Exception:
        logger.exception("Could not persist search analytics", extra={"request_id": request_id})

    if result.error_code == "validation_error":
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "validation_error",
                "message": result.formatted_response,
                "request_id": request_id,
            }},
            headers={"X-Request-ID": request_id},
        )
    return result


@app.get("/api/analytics/searches")
def analytics_searches(limit: int = Query(default=10000, ge=1, le=10000)):
    """Return recent flat search records for Power BI and other analytics clients."""
    return analytics_store.get_searches(limit=limit)


@app.get("/api/analytics/reliability")
def analytics_reliability():
    """Aggregate provider success rate, latency, and record counts."""
    return {"providers": analytics_store.provider_reliability()}


@app.get("/api/analytics/insights")
def analytics_insights():
    """Flight Intelligence & System Intelligence analytics from real stored records."""
    from engines.analytics_engine import build_insights
    return build_insights(
        analytics_store.get_searches(),
        analytics_store.get_provider_events(),
        analytics_store.get_interactions(),
    )


class InteractionPayload(BaseModel):
    event_type: str
    session_id: Optional[str] = None
    search_id: Optional[str] = None
    flight_key: Optional[str] = None
    provider: Optional[str] = None


_INTERACTION_TYPES = {
    "booking_link_displayed", "booking_link_clicked", "shortlist_added",
    "shortlist_removed", "comparison_initiated", "trip_saved", "trip_retrieved",
}


@app.post("/api/interactions")
def record_interaction(payload: InteractionPayload):
    """Record a real user interaction. Clicks are never bookings."""
    if payload.event_type not in _INTERACTION_TYPES:
        raise HTTPException(status_code=400, detail="Unknown event_type")
    analytics_store.record_interaction(
        payload.event_type, payload.session_id, payload.search_id,
        payload.flight_key, payload.provider,
    )
    return {"recorded": True, "event_type": payload.event_type}


@app.get("/api/analytics/interactions")
def analytics_interactions(limit: int = Query(default=10000, ge=1, le=10000)):
    return {"interactions": analytics_store.get_interactions(limit=limit)}


@app.post("/api/evidence/analyze")
def evidence_analyze(payload: FareAnalysisPayload):
    return analyze_evidence(payload.fare)


@app.post("/api/risk/analyze")
def risk_analyze(payload: FareAnalysisPayload, minimum_buffer: int = Query(120, ge=0, le=1440), maximum_layover: int = Query(720, ge=1, le=4320)):
    return analyze_transfer(payload.fare, min_buffer_minutes=minimum_buffer, max_layover_minutes=maximum_layover)


@app.post("/api/cost/estimate")
def cost_estimate(payload: CostPayload):
    return estimate_trip_cost(payload.fares, payload.expenses, payload.passengers)


@app.post("/api/recommendations/generate")
def recommendations_generate(payload: RecommendationsPayload):
    return generate_recommendations(payload.fares, payload.maximum_budget, payload.no_self_transfer)


@app.post("/api/alternatives/generate")
def alternatives_generate(fares: list[FareResult], limit: int = Query(3, ge=0, le=3)):
    return generate_alternatives(fares, limit=limit)


@app.post("/api/disruption/simulate")
def disruption_simulate(payload: DisruptionPayload):
    return simulate_disruption(payload.fare, payload.delay_minutes, payload.cancelled)


@app.post("/api/readiness/check")
def readiness_check(payload: ReadinessPayload):
    return check_readiness(payload.fare, payload.profile, cost=payload.cost)


def _session_id(request: Request) -> str:
    sid = request.cookies.get("t10_session", "")
    return sid if re.fullmatch(r"[a-f0-9]{32}", sid) else ""


@app.post("/api/trips/save")
def trips_save(payload: SaveTripPayload, request: Request):
    return trip_store.save(payload.payload, payload.title, session_id=_session_id(request) or None)


@app.get("/api/trips")
def trips_list(request: Request, limit: int = Query(100, ge=1, le=500)):
    return trip_store.list(limit=limit, session_id=_session_id(request) or None)


@app.get("/api/trips/{trip_id}")
def trips_get(trip_id: str, request: Request):
    trip = trip_store.get(trip_id, session_id=_session_id(request) or None)
    if not trip:
        return JSONResponse(status_code=404, content={"detail": "Saved trip not found."})
    return trip


@app.delete("/api/trips/{trip_id}")
def trips_delete(trip_id: str, request: Request):
    if not trip_store.delete(trip_id, session_id=_session_id(request) or None):
        return JSONResponse(status_code=404, content={"detail": "Saved trip not found."})
    return {"status": "deleted", "trip_id": trip_id}


@app.get("/api/trips/{trip_id}/report")
def trip_report(trip_id: str, request: Request, format: Literal["json", "csv"] = "json"):
    trip = trip_store.get(trip_id, session_id=_session_id(request) or None)
    if not trip:
        return JSONResponse(status_code=404, content={"detail": "Saved trip not found."})
    if format == "csv":
        import csv, io, json as _json
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["trip_id", "created_at", "title", "payload_json"])
        writer.writerow([trip["trip_id"], trip["created_at"], trip["title"], _json.dumps(trip["payload"], ensure_ascii=True)])
        return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="aerodecision-trip-{trip_id}.csv"'})
    return {"report_type": "saved_trip_decision_record", "booking_status": "not_booked_externally", "fare_and_availability_caveat": "Confirm final fare, baggage, seats, and availability with the provider.", **trip}


@app.post("/api/booking/options/{itinerary_id}")
def booking_options(itinerary_id: str, fare: FareResult):
    if not fare.booking_url:
        return {"itinerary_id": itinerary_id, "options": [], "status": "unavailable"}
    try:
        safe_url = validate_https_booking_url(fare.booking_url)
    except URLSafetyError:
        return {"itinerary_id": itinerary_id, "options": [], "status": "unsafe_or_unapproved_link"}
    return {"itinerary_id": itinerary_id, "options": [{"provider": fare.booking_provider or fare.source, "provider_type": fare.booking_provider_type or "unknown", "url": safe_url, "link_kind": fare.booking_link_kind, "fare": fare.price, "currency": fare.currency, "retrieved_at": fare.retrieved_at, "status": "booking_page_access_and_availability_unknown"}], "booking_completed": False}


@app.post("/api/session/reset")
def reset_session(request: Request, response: Response):
    """Clear the current browser conversation context."""
    session_id = request.cookies.get("t10_session", "")
    if re.fullmatch(r"[a-f0-9]{32}", session_id):
        session_memory_store.reset(session_id)
    else:
        session_id = uuid.uuid4().hex
    response.set_cookie(
        "t10_session",
        session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=30 * 24 * 60 * 60,
        path="/",
    )
    return {"status": "reset", "request_id": request.state.request_id}


if __name__ == "__main__":
    import os
    import shutil
    import subprocess
    import threading
    import webbrowser

    dashboard_url = "http://127.0.0.1:8000"

    def open_dashboard_in_brave():
        brave = shutil.which("brave.exe") or shutil.which("brave")
        if not brave and os.name == "nt":
            for install_dir in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
                if install_dir:
                    candidate = os.path.join(install_dir, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")
                    if os.path.isfile(candidate):
                        brave = candidate
                        break

        if brave:
            subprocess.Popen([brave, dashboard_url])
        else:
            webbrowser.open(dashboard_url)

    threading.Timer(1.5, open_dashboard_in_brave).start()
    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=False)
