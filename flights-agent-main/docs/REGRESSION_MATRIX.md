# Release regression matrix

Full automated suite: baseline before this pass was 135 passing tests; final count is recorded after the final run in `RELEASE_VALIDATION.md`. Every row below names a test that asserts the listed business result.

| # | Scenario | Test | Expected backend result and recommendation behavior |
|---|---|---|---|
| 1 | Fare returned, zero schedules | `test_agent_keeps_fare_only_result_separate_from_schedule_match` | Fare remains eligible and visible; schedule stays unconfirmed; fare-only recommendation may be selected. |
| 2 | Fare returned and schedule matched | `test_critical_matching_exact_match` | Exact route/date/flight match is returned; schedule evidence is confirmed for that record. |
| 3 | Provider unavailable | `test_agent_returns_provider_unavailable_status_when_both_providers_fail` | Both provider statuses are `error`, no candidate is selected, overall execution is `PROVIDER UNAVAILABLE`. |
| 4 | Partial provider success | `test_agent_partial_schedule_provider_failure_keeps_fare_recommendation` | Ignav fare remains selectable with schedule unconfirmed; overall status is partial data. |
| 5 | Different flights with identical fares | `test_cheapest_returns_all_equal_fares_and_keeps_currencies_separate` | Both same-currency flights appear in cheapest tie; foreign fare is not compared unless valid FX exists. |
| 6 | Route-level fare, no individual flight association | `test_ignav_preserves_route_level_fare_without_inventing_flight_number` | Provider record remains in unassociated results and is omitted from flight recommendation candidates. |
| 7 | Missing flight timings | `test_fastest_requires_valid_timestamps_and_qualifies_unmatched` | Fastest is `INSUFFICIENT DATA TO RANK`; missing duration is not treated as zero. |
| 8 | Provider-reported timings, unmatched schedule | `test_fastest_requires_valid_timestamps_and_qualifies_unmatched` | Timed result may rank as `FASTEST PROVIDER-REPORTED OPTION`, explicitly unconfirmed. |
| 9 | Multiple candidates with equal cheapest fares | `test_cheapest_returns_all_equal_fares_and_keeps_currencies_separate` | Every equal minimum is returned in `candidates`, with `tie=true`. |
| 10 | Insufficient connection-risk evidence | `test_lowest_risk_does_not_return_unassessed_winner` | No winner; label is `INSUFFICIENT DATA TO RANK`. |
| 11 | Equal evidence scores | `test_equal_evidence_scores_are_reported_as_ties` | All tied strongest candidates are returned and `tie=true`. |
| 12 | Strongest evidence selected correctly | `test_strongest_evidence_quality_returns_correct_candidate` | Higher backend evidence score is selected and its score/explanation are returned. |
| 13 | Currency-safe comparisons | `test_recommendations_normalize_currency_without_mutating_original_fare`; `test_fx_failure_keeps_same_currency_fares_rankable`, `test_recommendations_reuse_search_normalization_without_second_fx_call`; `test_fx_failure_does_not_hide_other_candidates_and_route_level_is_not_ranked` | Only same-currency or validly converted flight-specific fares compare; originals remain unchanged; failed conversions remain visible and incomparable. |
| 14 | Saved trips preserve original verification | `test_saved_trip_exposes_historical_evidence_without_rewriting_status`, `test_saved_trip_without_retrieval_timestamp_marks_freshness_unknown`, `test_newly_saved_matched_fare_and_schedule_show_source_ages`, `test_saved_fare_only_keeps_schedule_age_unestablished`, `test_old_fare_with_recent_schedule_is_still_historical_and_unmodified` | Historical status remains captured; age/revalidation fields report history without mutating original evidence. |
| 15 | Backend and frontend status semantics | `test_search_response_includes_composed_decision_workspace`; `test_web_app_index` | Search API returns centralized candidate status and the page renders the status/recommendation workspace from backend data. |

## Additional regression coverage

- Overnight elapsed time and timezone offsets: `test_fastest_requires_valid_timestamps_and_qualifies_unmatched`, `test_overnight_separate_ticket_risk_uses_available_layover`.
- Unknown baggage and additional cost: `test_trip_cost_retains_unknown_expenses`.
- Unsafe booking URL: `test_scraping_blocks_unapproved_redirect_before_request` in `test_scraping.py`.
- Historical evidence and restart persistence: `test_saved_trip_exposes_historical_evidence_without_rewriting_status`, `test_trip_store_persists_across_instances`.
- FX failure, stale rates, timeout, invalid rate, precision, multiple currencies, same currency, and weekend date allowance: `test_ecb_unavailable_rate_fails_closed`, `test_stale_ecb_rate_fails_closed`, `test_timeout_and_invalid_ecb_rate_fail_closed`, `test_ecb_currency_conversion_decimal_and_attribution`, `test_multiple_currency_conversion_uses_decimal`, `test_same_currency_does_not_call_provider`, `test_same_currency_comparison_is_identity_without_rate_observation`, `test_weekend_gap_is_accepted_by_four_calendar_day_policy`, `test_expired_cache_entry_is_refetched`, `test_unsupported_currency_is_distinguished_from_provider_outage`, `test_selected_comparison_currency_changes_normalized_cheapest`, `test_recommendations_reuse_search_normalization_without_second_fx_call`.
- Database migration compatibility: `test_legacy_analytics_schema_migration_preserves_rows_and_creates_backup` (legacy schema is upgraded additively and copied to backup). Malformed providers, duplicate fares, and invalid associations: `test_ignav_rejects_non_object_success_response`, `test_matching_deduplicates_duplicate_fare_records`, `test_matching_rejects_missing_flight_number`.

## External validation boundary

Ignav's published response contract describes an itinerary price, outbound/inbound legs or ordered legs, and segment-level flights. Its documentation says `verified` means a displayed fare or booking-price check supplied the amount; `unverified` means the amount has not been confirmed. This explains provider status, not independent validation by T10. A real authorized request was attempted with configured credentials, but outbound network access is blocked in this environment. No live response fixture was fabricated. Live Ignav semantics remain **EXTERNAL VALIDATION REQUIRED**.

## Starlette TestClient warning

The warning is emitted while importing `fastapi.testclient`: Starlette 1.8.0 deprecates its `httpx` compatibility path and recommends `httpx2`. Installed versions are FastAPI 0.143.2, Starlette 1.8.0, and httpx 0.28.1. This is the test harness, not an application request path. A safe replacement was not established by the installed requirements; no dependency was upgraded and no warning was suppressed. Starlette's [official TestClient guidance](https://www.starlette.io/testclient/) recommends `httpx2` (included in `starlette[full]`). A dependency migration should add that supported test dependency and rerun the full suite; the current environment does not have `httpx2`, so this release leaves the warning visible.
