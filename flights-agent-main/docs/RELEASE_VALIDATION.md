# Final release validation

## Repository and starting point

Repository audit covered the provider clients and parser, normalized models, search orchestration, matching/ranking, FX service, decision engines, saved-trip storage, API schemas, frontend embedded in `web_app.py`, tests, documentation, and current SQLite schema. Before edits, `pytest -q` reported **135 passed, 0 failed, 1 warning**. The user-provided 133 count was stale. Existing source files and SQLite data were left in place; automated tests use temporary databases.

## Implemented and tested

- Search budget currency is the explicit comparison currency. With no selected currency, `DEFAULT_COMPARISON_CURRENCY` is used (default INR).
- FX uses ECB EUR-reference rates and Decimal calculations. Source fare/currency remain untouched; normalized amount/currency, source, observation date, and retrieval time are separate fields.
- Freshness limit is `ECB_FX_MAX_AGE_DAYS` (default 4 calendar days, allowed range 1-7), including ordinary weekend gaps. Same-currency identity needs no request. Unsupported, invalid, stale, timeout, and missing rates fail closed. Same-currency candidates can still rank and an individual conversion failure does not hide other usable results.
- Frontend provenance displays original fare, comparison amount, conversion status, ECB source, and observation date. A visible ECB reference-rate disclaimer warns that real booking charges can differ.
- Saved-trip views add original fare and schedule status, per-source evidence timestamps and ages, aggregate evidence age, current validation status, and `revalidation_occurred=false`; time does not rewrite original status. Missing provider timestamps remain unknown even when a search timestamp exists.
- Ignav records without an individual flight number remain separate, retain relevant source fields, and are excluded from flight ranking. Itinerary fare records preserve original price/status and segment fields. Recommendation generation explicitly accepts only flight-specific and itinerary-specific associations. FX-normalized values computed during search are reused in the decision workspace so the displayed comparison and ranking use the same backend calculation.
- Regression additions cover equal evidence ties, strongest-candidate selection, stale/failing FX isolation, provider failure/partial success, and saved trip fields. Full mapping is in `REGRESSION_MATRIX.md`.
- No schema migration was needed. Existing analytics schema and saved-trip migration version remain unchanged; persistence changes are computed response fields only.

## Ignav semantics

Published [Ignav response-format docs](https://ignav.com/docs/response-format) describe a price object with amount, currency, and status, plus outbound/inbound itinerary legs or ordered legs and individual flight segments. The docs define `verified` as a price coming from a displayed fare or booking-price check; `unverified` means the amount has not been confirmed. Provider `verified` status is not T10 independent verification, current seat availability, or a booking guarantee.

Credentials are configured, but an authorized representative request was attempted and blocked by the execution environment's network policy (`WinError 10013`). There is no genuine response to sanitize into a fixture. Live fare-semantics validation is **IMPLEMENTED — EXTERNAL VALIDATION REQUIRED**. Do not claim live provider semantics have been confirmed.

## Warning investigation

The sole warning is `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` It originates from `fastapi/testclient.py` importing Starlette TestClient. Installed versions: FastAPI 0.143.2, Starlette 1.8.0, httpx 0.28.1. This is the test-client integration, not application HTTP behavior. Starlette [officially recommends `httpx2`](https://www.starlette.io/testclient/), included in the `starlette[full]` extra. The installed environment lacks it. Add the documented test dependency and rerun the suite as a compatibility change; do not silence the warning.

## Files changed in this pass

`agent.py`, `config.py`, `ignav_client.py`, `models.py`, `ranking.py`, `web_app.py`, `engines/recommendation_engine.py`, `services/decision_service.py`, `services/fx_service.py`, `services/trip_service.py`, `.env.example`, `README.md`, `tests/test_agent.py`, `tests/test_analytics.py`, `tests/test_config.py`, `tests/test_decision_engines.py`, `tests/test_decision_service.py`, `tests/test_fx_service.py`, `tests/test_ignav.py`, `tests/test_ranking.py`, `tests/test_trip_service.py`, `tests/test_web_app.py`, `docs/REGRESSION_MATRIX.md`, and this report. No application database schema migration was required.

## Status classification

- **IMPLEMENTED AND TESTED:** ECB recommendation comparisons and policy, fail-closed conversion, provenance display, route/airline separation, saved-trip history fields, recommendation tie behavior, regression matrix, dashboard smoke, legacy analytics migration compatibility.
- **IMPLEMENTED ? EXTERNAL VALIDATION REQUIRED:** Ignav semantics mapping based on published documentation and sanitized local parser tests; live response verification is blocked by network policy.
- **NOT IMPLEMENTED:** Live schedule/fare validation, automated browser-driven interaction, and actual trip revalidation against providers. The system reports no revalidation instead of asserting freshness.
- **BLOCKED ? EXPLAIN WHY:** Direct authorized Ignav request could not connect because outbound sockets are blocked (`WinError 10013`). No live response fixture was fabricated.

## Validation not established

- Live AviationStack and Ignav responses could not be requested due to outbound network blocking; provider subscription/market behavior still requires an external authorized run.
- Browser behavior is checked through the FastAPI page/API integration tests; no browser automation runtime is configured.
- There is no migration framework beyond additive `schema_migrations`; no database migration was needed for this change.
- Final validation: `pytest -q` reports **161 passed, 0 failed, 0 skipped, 1 warning**. A temporary-database Uvicorn smoke run returned dashboard HTTP 200, found the expected title, and rendered the currency selector. `git diff --check` is run before delivery.
