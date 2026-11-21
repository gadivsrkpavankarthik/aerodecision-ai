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
    <title>T10 Flight Option Finder</title>
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
            <h1 class="text-3xl font-black text-slate-900 tracking-tight">T10 Flight Option Finder</h1>
            <p class="text-slate-500 mt-1.5 text-xs sm:text-sm max-w-xl mx-auto font-medium">Real flight schedules via AviationStack, live fares via Ignav, deterministic matching & smart recommendations.</p>
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
            <!-- Best Option Card -->
            <div id="bestOptionCard" class="bg-white rounded-2xl border border-slate-200/90 shadow-sm overflow-hidden">
                <div class="p-8 text-center text-slate-400 text-sm">
                    Select origin, destination and date above to find the best verified flight.
                </div>
            </div>
            <section id="decisionWorkspaceCard" class="hidden bg-white rounded-2xl border border-slate-200/90 shadow-sm p-6">
                <h2 class="text-sm font-bold text-slate-800">Decision workspace</h2>
                <div id="decisionRecommendations" class="mt-4 grid gap-3 md:grid-cols-2"></div>
                <div class="mt-5 flex flex-wrap items-center gap-2"><span class="text-xs font-semibold text-slate-600">Delay what-if:</span><div id="disruptionButtons" class="flex flex-wrap gap-2"></div><button id="readinessButton" class="px-3 py-2 rounded-lg bg-indigo-50 text-indigo-700 text-xs font-semibold">Check readiness</button></div>
                <div id="decisionActionResult" class="mt-3 text-xs text-slate-600" aria-live="polite"></div>
            </section>

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
                <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wider mb-4">Candidate Fares & Real Matching Status</h3>
                <table class="w-full text-left text-xs text-slate-600">
                    <thead class="bg-slate-50 text-slate-700 uppercase font-semibold border-b border-slate-200 text-[11px]">
                        <tr>
                            <th class="px-4 py-3">Flight</th>
                            <th class="px-4 py-3">Airline</th>
                            <th class="px-4 py-3">Route</th>
                            <th class="px-4 py-3">Schedule</th>
                            <th class="px-4 py-3">Price</th>
                            <th class="px-4 py-3">Fare Status</th>
                            <th class="px-4 py-3">Schedule Match</th>
                        </tr>
                    </thead>
                    <tbody id="candidatesTableBody" class="divide-y divide-slate-100 font-mono text-[11px]">
                        <tr>
                            <td colspan="7" class="px-4 py-6 text-center text-slate-400 font-sans italic">No fare search executed yet.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    <section class="mt-8 bg-white rounded-2xl border border-slate-200 p-6">
        <div class="flex items-center justify-between gap-3"><h2 class="font-bold text-slate-800">Saved decision workspace</h2><button onclick="loadSavedTrips()" class="px-3 py-2 text-xs rounded-lg bg-slate-100 hover:bg-slate-200">Refresh saved trips</button></div>
        <div id="savedTrips" class="mt-3 text-sm text-slate-500">Save a selected result to keep its search evidence and review it later.</div>
    </section>

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

                stepperBadge.innerText = data.execution_status || "NO RESULTS";
                stepperBadge.className = "text-[10px] font-mono font-bold px-2.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200";

                renderResults(data);
                window.t10LastSearch = data;
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

        async function saveBestTrip() {
            const data=window.t10LastSearch;
            if (!data || !data.best_flight) return;
            const result=await fetch('/api/trips/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:`${data.best_flight.origin} to ${data.best_flight.destination} - ${data.best_flight.travel_date}`,payload:{search_request:data.search_request,selected_itinerary:data.best_flight,alternatives:data.decision_workspace?.alternatives,decision_workspace:data.decision_workspace,request_id:data.request_id,searched_at:data.searched_at}})});
            if (!result.ok) { alert('Could not save this trip.'); return; }
            await loadSavedTrips();
        }
        async function loadSavedTrips() {
            const target=document.getElementById('savedTrips');
            try { const response=await fetch('/api/trips'); const trips=await response.json(); target.replaceChildren(); if (!trips.length) { target.textContent='No saved trips yet.'; return; }
                trips.forEach(trip=>{ const row=document.createElement('div'); row.className='flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 py-3'; const label=document.createElement('span'); const ages=trip.evidence_age_by_source || {}; label.textContent=`${trip.title} - ${new Date(trip.created_at).toLocaleString()} · Original: ${trip.original_evidence_status || 'UNKNOWN'} · Fare ${ages.fare || 'FRESHNESS NOT ESTABLISHED'} · Schedule ${ages.schedule || 'FRESHNESS NOT ESTABLISHED'} · ${trip.revalidation_occurred ? 'Revalidated' : 'Not revalidated'}`; const link=document.createElement('a'); link.href=`/api/trips/${encodeURIComponent(trip.trip_id)}/report`; link.target='_blank'; link.rel='noopener noreferrer'; link.className='text-blue-700 underline text-xs'; link.textContent='Open decision record'; row.append(label,link); target.append(row); });
            } catch (_) { target.textContent='Saved trips could not be loaded.'; }
        }
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
                    '<span class="text-xs uppercase tracking-widest font-semibold bg-white/20 px-3 py-1 rounded-full">✈️ ' + escapeHtml(best.schedule_matched ? 'MATCHED FLIGHT AND FARE' : 'BEST AVAILABLE FARE CANDIDATE') + '</span>',
                    '<span class="text-xs font-medium px-2.5 py-0.5 rounded-md badge-unverified">' + escapeHtml(best.trust_status?.overall || (isSelfTransfer ? 'SELF-TRANSFER ESTIMATE' : best.schedule_matched ? 'MATCHED FARE AND SCHEDULE' : 'FARE ONLY')) + '</span>',
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
                textParts.push('<button onclick="saveBestTrip()" class="px-4 py-2 bg-slate-800 text-white rounded-lg text-xs">Save trip</button>');
                if (bookingUrl && !isSelfTransfer) {
                    textParts.push('<a href="' + escapeHtml(bookingUrl) + '" target="_blank" rel="noopener noreferrer" class="w-full sm:w-auto px-6 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl text-xs shadow transition text-center whitespace-nowrap">Search ' + escapeHtml(best.booking_provider || 'Airline') + ' →</a>');
                } else {
                    textParts.push('<span class="text-xs text-slate-500">' + escapeHtml(isSelfTransfer ? 'No single booking link — book each leg separately.' : 'Booking link unavailable') + '</span>');
                }
                textParts.push('</div>');
                const workspace = data.decision_workspace || {};
                const evidence = workspace.evidence?.[best.flight_number];
                const risk = workspace.connection_risk?.[best.flight_number];
                const cost = workspace.cost || {};
                textParts.push('<div class="px-6 pb-6 grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">');
                textParts.push('<div class="rounded-lg bg-slate-50 p-3"><b>Evidence quality</b><div>' + escapeHtml(evidence ? `${evidence.evidence_score}/100 · ${evidence.evidence_status}` : 'Unavailable') + '</div></div>');
                textParts.push('<div class="rounded-lg bg-slate-50 p-3"><b>Connection assessment</b><div>' + escapeHtml(risk ? `${risk.itinerary_type === 'direct' ? 'DIRECT ITINERARY CANDIDATE' : risk.category.toUpperCase()} · risk assessment incomplete` : 'INSUFFICIENT DATA') + '</div></div>');
                textParts.push('<div class="rounded-lg bg-slate-50 p-3"><b>Known cost · not complete trip total</b><div>' + escapeHtml(cost.known_cost == null ? 'Unavailable' : formatMoney(cost.known_cost, cost.currency || req.currency || 'INR')) + '</div><small>Complete cost ' + escapeHtml(cost.complete_trip_cost == null ? `not yet determined · ${cost.unknown_expenses?.length || 0} unknown charges` : formatMoney(cost.complete_trip_cost, cost.currency || req.currency || 'INR')) + '</small></div>');
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

            const tb = document.getElementById('candidatesTableBody');
            const items = data.matched_fares || data.fares_found || [];
            if (items.length > 0) {
                tb.replaceChildren(...items.slice(0, 15).map(f => {
                    const row = document.createElement('tr');
                    row.className = 'hover:bg-slate-50 transition';
                    const cells = [
                        f.flight_number,
                        f.airline || '-',
                        `${f.origin} → ${f.destination}`,
                        `${f.departure_time || '-'} - ${f.arrival_time || '-'}`,
                        f.price ? formatMoney(f.price, f.currency || req.currency || 'INR') : 'Unavailable',
                        f.trust_status?.fare || 'FARE RECEIVED',
                        f.schedule_matched ? '✓ MATCH' : f.price_available ? 'FARE ONLY' : '✕ NO FARE',
                    ];
                    cells.forEach((value, idx) => {
                        const cell = document.createElement(idx === 1 ? 'td' : 'td');
                        cell.className = idx === 0 ? 'px-4 py-3 font-semibold text-slate-800' : idx === 1 ? 'px-4 py-3 font-sans' : idx === 4 ? 'px-4 py-3 font-semibold text-slate-900' : idx === 5 ? 'px-4 py-3' : idx === 6 ? 'px-4 py-3 font-semibold ' + (f.schedule_matched ? 'text-emerald-600' : f.price_available ? 'text-blue-600' : 'text-slate-400') : 'px-4 py-3';
                        if (idx === 5) {
                            const badge = document.createElement('span');
                            badge.className = 'px-2 py-0.5 rounded text-[10px] font-semibold uppercase badge-unverified';
                            badge.title = f.itinerary_kind === 'connecting_self_transfer' ? 'Estimated sum of separate leg fares; no through-ticket is confirmed.' : '';
                            badge.textContent = value;
                            cell.appendChild(badge);
                        } else {
                            cell.textContent = value;
                        }
                        row.appendChild(cell);
                    });
                    const provenance=document.createElement('details'); provenance.className='mt-1 text-[10px] font-sans';
                    const summary=document.createElement('summary'); summary.className='cursor-pointer text-blue-700'; summary.textContent='Evidence details'; provenance.append(summary);
                    const info=data.decision_workspace?.evidence?.[f.flight_number] || {};
                    const block=document.createElement('div'); block.className='mt-2 space-y-1 whitespace-normal';
                    const lines=[`Original provider fare: ${f.price == null ? 'Unavailable' : formatMoney(f.price,f.currency || 'UNKNOWN')}`,`Comparison amount: ${f.comparison_amount ? `${f.comparison_amount} ${f.comparison_currency}` : 'Unavailable'}`,`Conversion status: ${f.fx_status || 'unavailable'}`,`Conversion source: ${f.fx_rate_source || 'Not applicable or unavailable'}`,`Rate observation date: ${f.fx_observation_date || 'Not available'}`,`Association: ${f.association_level || 'flight-specific provider record'}`,`Fare source: ${info.fare_source || f.source || 'UNKNOWN'}`,`Fare retrieved: ${f.retrieved_at || 'UNKNOWN'}`,`Schedule: ${f.trust_status?.schedule || 'SCHEDULE UNCONFIRMED'}`,`Timing: ${f.departure_time || 'UNKNOWN'} - ${f.arrival_time || 'UNKNOWN'} (${f.schedule_matched ? 'matched schedule' : 'provider-reported; unconfirmed'})`,`Evidence score: ${info.evidence_score ?? 'UNKNOWN'}`,`Booking: ${f.booking_provider || 'UNKNOWN'} · ${f.booking_url ? 'link supplied; not validated here' : 'NOT AVAILABLE'}`,`Seat availability: ${f.trust_status?.availability || 'NOT CONFIRMED'}`,`Unknown: baggage, terminal`];
                    lines.forEach(line=>{const item=document.createElement('div'); item.textContent=line; block.append(item);}); provenance.append(block);
                    row.lastElementChild.append(provenance);
                    return row;
                }));
            } else {
                tb.innerHTML = '<tr><td colspan="7" class="px-4 py-6 text-center text-slate-400 font-sans italic">No candidate fares available.</td></tr>';
            }
        }

        function renderDecisionWorkspace(data) {
            const panel=document.getElementById('decisionWorkspaceCard');
            const target=document.getElementById('decisionRecommendations');
            const actions=document.getElementById('decisionActionResult');
            const workspace=data.decision_workspace || {};
            const categories=workspace.recommendations?.categories || {};
            const alternatives=workspace.alternatives || [];
            if (!data.best_flight && !(workspace.route_level_fares || []).length) { panel.classList.add('hidden'); return; }
            panel.classList.remove('hidden'); target.replaceChildren();
            Object.entries(categories).forEach(([name,item])=>{
                const card=document.createElement('article'); card.className='rounded-xl border border-slate-100 bg-slate-50 p-3';
                const heading=document.createElement('h3'); heading.className='text-xs font-bold text-slate-800'; heading.textContent=name.replaceAll('_',' ').replace(/\\b\\w/g,c=>c.toUpperCase());
                const detail=document.createElement('p'); detail.className='mt-1 text-xs text-slate-600'; const selected=item.flight || {}; detail.textContent=`${selected.flight_number || 'Flight'} · Original ${selected.price ?? 'unknown'} ${selected.currency || ''}${selected.comparison_amount ? ` · Comparison ${selected.comparison_amount} ${selected.comparison_currency} (${selected.fx_status}; ${selected.fx_rate_source || 'identity'}; observed ${selected.fx_observation_date || 'n/a'})` : ''}. ${item.explanation || ''}`;
                card.append(heading,detail); target.append(card);
            });
            if (alternatives.length) {
                const list=document.createElement('p'); list.className='md:col-span-2 text-xs text-slate-600';
                list.textContent='Provider-returned alternatives: '+alternatives.map(x=>`${x.itinerary.flight_number} (${x.itinerary.price ?? 'unknown'} ${x.itinerary.currency || ''})`).join(', ');
                target.append(list);
            }
            (workspace.route_level_fares || []).forEach(quote=>{const box=document.createElement('div');box.className='md:col-span-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs';box.textContent=`ROUTE-LEVEL FARE INDICATION — ${quote.source || 'Provider'} · ${quote.origin || 'UNKNOWN'} to ${quote.destination || 'UNKNOWN'} · ${quote.price ?? 'UNKNOWN'} ${quote.currency || ''}. Not associated with an individual flight. ${quote.reason || ''}`;target.append(box);});
            const buttons=document.getElementById('disruptionButtons'); buttons.replaceChildren();
            [15,30,60,90,120].forEach(minutes=>{ const button=document.createElement('button'); button.type='button'; button.className='px-3 py-2 rounded-lg bg-amber-50 text-amber-800 text-xs'; button.textContent=`+${minutes} min`; button.addEventListener('click',()=>runDisruptionScenario(minutes)); buttons.append(button); });
            document.getElementById('readinessButton').onclick=runReadinessCheck;
            actions.textContent=workspace.limitations?.join(' ') || '';
            if (workspace.recommendations?.fx_disclaimer) actions.textContent += ' ' + workspace.recommendations.fx_disclaimer;
        }

        async function runDisruptionScenario(minutes) {
            const fare=window.t10LastSearch?.best_flight; if (!fare) return;
            const target=document.getElementById('decisionActionResult'); target.textContent='Calculating scenario…';
            try { const response=await fetch('/api/disruption/simulate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fare,delay_minutes:minutes})}); const data=await response.json();
                target.textContent=`${data.scenario}: remaining connection buffer ${data.remaining_connection_buffer_minutes ?? 'unknown'} minutes; connection feasibility ${data.connection_feasible == null ? 'not applicable or unknown' : data.connection_feasible ? 'feasible by time only' : 'not feasible by time'}. No actual delay is predicted.`;
            } catch (_) { target.textContent='Scenario analysis is unavailable.'; }
        }

        async function runReadinessCheck() {
            const fare=window.t10LastSearch?.best_flight; if (!fare) return;
            const target=document.getElementById('decisionActionResult'); target.textContent='Checking readiness…';
            try { const response=await fetch('/api/readiness/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fare,cost:window.t10LastSearch.decision_workspace?.cost})}); const data=await response.json();
                target.textContent=`Readiness ${data.ready ? 'complete' : 'needs review'}; unresolved: ${(data.failed_or_unconfirmed || []).join(', ') || 'none'}.`;
            } catch (_) { target.textContent='Readiness check is unavailable.'; }
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


@app.post("/api/trips/save")
def trips_save(payload: SaveTripPayload):
    return trip_store.save(payload.payload, payload.title)


@app.get("/api/trips")
def trips_list(limit: int = Query(100, ge=1, le=500)):
    return trip_store.list(limit)


@app.get("/api/trips/{trip_id}")
def trips_get(trip_id: str):
    trip = trip_store.get(trip_id)
    if not trip:
        return JSONResponse(status_code=404, content={"detail": "Saved trip not found."})
    return trip


@app.get("/api/trips/{trip_id}/report")
def trip_report(trip_id: str, format: Literal["json", "csv"] = "json"):
    trip=trip_store.get(trip_id)
    if not trip:
        return JSONResponse(status_code=404, content={"detail": "Saved trip not found."})
    if format == "csv":
        import csv, io
        output=io.StringIO(); writer=csv.writer(output); writer.writerow(["trip_id","created_at","title","payload_json"]); writer.writerow([trip['trip_id'],trip['created_at'],trip['title'],__import__('json').dumps(trip['payload'],ensure_ascii=True)])
        return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="t10-trip-{trip_id}.csv"'})
    return {"report_type":"saved_trip_decision_record","booking_status":"not_booked_externally","fare_and_availability_caveat":"Confirm final fare, baggage, seats, and availability with the provider.",**trip}


@app.post("/api/booking/options/{itinerary_id}")
def booking_options(itinerary_id: str, fare: FareResult):
    if not fare.booking_url:
        return {"itinerary_id":itinerary_id,"options":[],"status":"unavailable"}
    try:
        safe_url=validate_https_booking_url(fare.booking_url)
    except URLSafetyError:
        return {"itinerary_id":itinerary_id,"options":[],"status":"unsafe_or_unapproved_link"}
    return {"itinerary_id":itinerary_id,"options":[{"provider":fare.booking_provider or fare.source,"provider_type":fare.booking_provider_type or "unknown","url":safe_url,"link_kind":fare.booking_link_kind,"fare":fare.price,"currency":fare.currency,"retrieved_at":fare.retrieved_at,"status":"booking_page_access_and_availability_unknown"}],"booking_completed":False}


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
    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=False)
