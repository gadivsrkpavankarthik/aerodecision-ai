# T10 implementation inspection and plan

## Existing implementation

- FastAPI serves the browser search interface and `/api/search`; the agent queries AviationStack and Ignav concurrently, optionally interprets requests with NVIDIA NIM, validates constraints, and returns traceable results.
- Pydantic models already distinguish schedule, fare, provider availability, booking-page checks, and fare/schedule evidence. Matching normalizes flight numbers, checks carrier, route, date, stops and departure time, and keeps unmatched records visible.
- Deterministic ranking offers cheapest, fastest, balanced, and lower-transfer-concern profiles. Domestic self-transfer synthesis uses four configured hubs and timezone-aware timestamp validation.
- `connection_utils.py` includes rule-based transfer classification and a preliminary cost breakdown. `scraping/` checks links on an HTTPS hostname allowlist and validates redirect destinations/IP resolution.
- `analytics.py` stores flat search records in SQLite with schema version 1 and makes a backup before adding legacy columns. No separate migration framework exists.
- Tests cover the existing search workflow, providers, validation, matching, ranking, transfer timing, booking-page evidence, and analytics. Notebook generation is present; there is no Power BI report artifact.

## Partial and missing capabilities

- Evidence is assembled during matching but there is no independent explainable evidence score/history engine.
- Transfer checks cover timing and coarse concern labels; baggage, terminals, immigration, downstream recovery, and user buffers are not modeled.
- Cost helper supports fares and user expenses but not itemized categories, unknown amounts, per-passenger semantics, or currency conversion.
- Existing ranking does not produce five category recommendations or score explanations/Pareto comparisons.
- No automatic alternatives service, disruption simulation, readiness profiles/checklists, saved trip workspace, PDF/CSV reports, or analytics metrics for the new intelligence modules.
- Provider links are exposed in search results and the verifier can synthesize a generic search link. There is no dedicated booking options API or persistent link-check history. The verifier is intentionally restrictive and does not constitute availability or booking confirmation.
- The frontend is an embedded single-page search/results interface; the new decision workspace does not exist.

## Reuse and change plan

- Reuse `FareResult`, `FlightEvidence`, `SearchRequest`, `ItineraryLeg`, `connection_utils`, existing provider clients, `rank_fares`, `SearchAnalyticsStore`, and the current UI/API.
- Add pure deterministic engines under `engines/` for evidence, transfer risk, costs, recommendations, alternatives, disruption, and readiness.
- Add SQLite saved-trip and report services under `services/`, using additive tables and schema versioning. Avoid storing payment data or unnecessary passenger identity.
- Add typed request models and additive `/api/...` routes in `web_app.py`; retain `/api/search`, current response models, and analytics shape for compatibility.
- Expose essential analysis and save/report workflows in the existing browser page using real API results. Keep provider-dependent alternatives explicitly unavailable unless schedule/fare records actually support them.
- Direct booking handoff will use provider-supplied links or clearly labeled generic fallbacks; validate destinations before backend checks, preserve deep-link queries, and never infer booking completion.

## Risks, requirements, and compatibility

- Live schedule/fare availability requires configured AviationStack and Ignav credentials and provider plan access. Current results cannot promise inventory or final checkout totals.
- Currency conversion requires a configured, attributable exchange-rate source; until integrated, cross-currency totals remain unknown rather than converted.
- Visa/immigration, terminal, baggage, subsequent-flight, and airport-transfer facts require authoritative source data. Missing facts must remain unknown.
- PDF output may require a new dependency; CSV and JSON reports can be delivered without one. The local app has no authentication, so saved-trip/report routes need opaque identifiers and must not be exposed publicly without access controls.
- SQLite changes must preserve existing `searches`; new tables are additive. Existing frontend/API calls remain valid.

## Staged implementation plan

1. Establish typed engine contracts and additive SQLite migrations.
2. Implement evidence, transfer-risk, cost, and explainable recommendation engines with unit tests.
3. Implement alternative planning, disruption scenarios, and readiness checks using only supplied/provider-backed data.
4. Add persisted trips, comparison/export, and report generation.
5. Add booking-options validation/handoff and API surface.
6. Integrate analysis and saved-trip actions into the current frontend.
7. Extend analytics/docs, run the complete test suite, and report live-provider limitations.

This inspection found working Phase 1 functionality; the implementation extends it rather than replacing it.
