"""SQLite-backed search history for Power BI and other analytics clients."""

from __future__ import annotations

import json
import os
from contextlib import closing
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import FlightSearchResult


class SearchAnalyticsStore:
    """Persist one flat analytics record for each completed search."""

    CURRENT_SCHEMA_VERSION = 1
    STANDARD_FIELDS = {
        "search_id": None,
        "request_id": None,
        "searched_at": None,
        "recorded_at": None,
        "origin": None,
        "origin_city": None,
        "destination": None,
        "destination_city": None,
        "travel_date": None,
        "budget": None,
        "budget_currency": None,
        "passengers": None,
        "cabin_class": None,
        "preferred_airline": None,
        "preferred_departure": None,
        "schedule_count": None,
        "fare_count": None,
        "candidate_count": None,
        "eligible_count": None,
        "rejected_count": None,
        "recommendation_count": None,
        "matched_schedule_count": None,
        "verified_fare_count": None,
        "unverified_fare_count": None,
        "flight_found": None,
        "best_flight_number": None,
        "best_airline": None,
        "best_price": None,
        "fare_currency": None,
        "fare_status": None,
        "availability_status": None,
        "fare_retrieved_at": None,
        "schedule_matched": None,
        "schedule_retrieved_at": None,
        "lowest_fare_in_budget_currency": None,
        "selected_fare_verified": None,
        "budget_met": None,
        "budget_utilization_pct": None,
        "search_outcome": None,
        "stops": None,
        "itinerary_kind": None,
        "booking_provider": None,
        "web_verification_status": None,
        "search_duration_ms": None,
        "aviationstack_status": None,
        "ignav_status": None,
        "aviationstack_duration_ms": None,
        "ignav_duration_ms": None,
        "error_code": None,
    }

    def __init__(self, db_path: str | Path | None = None):
        default_path = Path(__file__).resolve().parent / "flight_analytics.sqlite3"
        self.db_path = Path(db_path or os.getenv("ANALYTICS_DB_PATH", default_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _backup_db(self) -> None:
        if not self.db_path.exists():
            return
        backup_path = self.db_path.with_suffix(self.db_path.suffix + ".bak")
        backup_candidate = backup_path
        counter = 1
        while backup_candidate.exists():
            backup_candidate = self.db_path.with_name(f"{self.db_path.stem}-backup-{counter}{self.db_path.suffix}.bak")
            counter += 1
        shutil.copy2(self.db_path, backup_candidate)

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS searches ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "searched_at TEXT NOT NULL, "
                "record_json TEXT NOT NULL, "
                "schema_version INTEGER NOT NULL DEFAULT 1)"
            )
            columns = [row[1] for row in connection.execute("PRAGMA table_info(searches)").fetchall()]
            if "schema_version" not in columns:
                self._backup_db()
                connection.execute("ALTER TABLE searches ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            if "record_json" not in columns:
                self._backup_db()
                connection.execute("ALTER TABLE searches ADD COLUMN record_json TEXT NOT NULL DEFAULT '{}' ")
            if "fares_json" not in columns:
                self._backup_db()
                connection.execute("ALTER TABLE searches ADD COLUMN fares_json TEXT NOT NULL DEFAULT '[]' ")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "search_id TEXT NOT NULL, "
                "provider TEXT NOT NULL, "
                "status TEXT, "
                "duration_ms INTEGER, "
                "record_count INTEGER, "
                "error_category TEXT, "
                "recorded_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_provider_events_provider ON provider_events(provider)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS interaction_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_id TEXT NOT NULL, "
                "session_id TEXT, "
                "search_id TEXT, "
                "flight_key TEXT, "
                "event_type TEXT NOT NULL, "
                "provider TEXT, "
                "recorded_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_interaction_events_type ON interaction_events(event_type)"
            )
            connection.commit()

    def _normalize_record(self, record: dict[str, Any] | None) -> dict[str, Any]:
        normalized = dict(self.STANDARD_FIELDS)
        if not isinstance(record, dict):
            return normalized
        for key in normalized:
            value = record.get(key)
            if value is None and key in {"search_outcome", "availability_status"}:
                normalized[key] = "unknown" if key == "availability_status" else "no_result"
            else:
                normalized[key] = value
        return normalized

    def record_search(self, result: FlightSearchResult, duration_ms: int) -> None:
        request = result.search_request
        best = result.best_flight
        record: dict[str, Any] = {
            "search_id": str(uuid.uuid4()),
            "request_id": result.request_id,
            "searched_at": result.searched_at,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "origin": request.origin_code if request else None,
            "origin_city": request.origin_city if request else None,
            "destination": request.destination_code if request else None,
            "destination_city": request.destination_city if request else None,
            "travel_date": request.travel_date if request else None,
            "budget": request.budget if request else None,
            "budget_currency": request.currency if request else None,
            "passengers": request.passengers if request else None,
            "cabin_class": request.cabin_class if request else None,
            "preferred_airline": request.preferred_airline if request else None,
            "preferred_departure": request.preferred_departure if request else None,
            "schedule_count": len(result.flights_found),
            "fare_count": len(result.fares_found),
            "candidate_count": len(result.valid_candidates) + sum(
                1 for fare in result.matched_fares
                if fare.constraint_status and fare.constraint_status != "eligible"
            ),
            "eligible_count": len(result.valid_candidates),
            "rejected_count": sum(
                1 for fare in result.matched_fares
                if fare.constraint_status and fare.constraint_status != "eligible"
            ),
            "recommendation_count": len(result.recommendations or []),
            "matched_schedule_count": sum(fare.schedule_matched for fare in result.matched_fares),
            "verified_fare_count": sum(
                fare.price_available and fare.price_status == "verified"
                for fare in result.fares_found
            ),
            "unverified_fare_count": sum(
                fare.price_available and fare.price_status == "unverified"
                for fare in result.fares_found
            ),
            "flight_found": best is not None,
            "best_flight_number": best.flight_number if best else None,
            "best_airline": best.airline if best else None,
            "best_price": best.price if best else None,
            "fare_currency": best.currency if best else None,
            "fare_status": best.price_status if best else None,
            "availability_status": best.availability_status if best else None,
            "fare_retrieved_at": best.retrieved_at if best else None,
            "schedule_matched": best.schedule_matched if best else None,
            "schedule_retrieved_at": best.schedule_retrieved_at if best else None,
            "lowest_fare_in_budget_currency": min(
                (
                    fare.price for fare in result.fares_found
                    if fare.price is not None
                    and fare.currency
                    and fare.currency.upper() == (request.currency if request else "").upper()
                ),
                default=None,
            ),
            "selected_fare_verified": bool(best and best.price_status == "verified"),
            "budget_met": (
                best.price <= request.budget
                if best and best.price is not None and request and request.budget is not None
                and best.currency and best.currency.upper() == request.currency
                else None
            ),
            "budget_utilization_pct": (
                round(best.price / request.budget * 100, 2)
                if best and best.price is not None and request and request.budget
                and best.currency and best.currency.upper() == request.currency
                else None
            ),
            "search_outcome": (
                "validation_error"
                if result.error_code == "validation_error"
                else "result_found" if best
                else "provider_error"
                if any(status in ("error", "malformed_response") for status in result.provider_statuses.values())
                else "no_result"
            ),
            "stops": best.stops if best else None,
            "itinerary_kind": best.itinerary_kind if best else None,
            "booking_provider": best.booking_provider if best else None,
            "web_verification_status": best.web_verification_status if best else None,
            "search_duration_ms": max(0, int(duration_ms)),
            "aviationstack_status": result.provider_statuses.get("aviationstack"),
            "ignav_status": result.provider_statuses.get("ignav"),
            "aviationstack_duration_ms": result.provider_durations_ms.get("aviationstack"),
            "ignav_duration_ms": result.provider_durations_ms.get("ignav"),
            "error_code": result.error_code,
        }
        # Compact per-fare snapshots for fare-level analytics. Native price and
        # currency are preserved; comparison fields carry FX-normalized values.
        fares_snapshot = [
            {
                "flight_number": f.flight_number,
                "airline": f.airline,
                "airline_code": f.airline_code,
                "origin": f.origin,
                "destination": f.destination,
                "travel_date": f.travel_date,
                "departure_time": f.departure_time,
                "arrival_time": f.arrival_time,
                "departure_at": f.departure_at,
                "arrival_at": f.arrival_at,
                "price": f.price,
                "currency": f.currency,
                "comparison_amount": f.comparison_amount,
                "comparison_currency": f.comparison_currency,
                "stops": f.stops,
                "itinerary_kind": f.itinerary_kind,
                "schedule_matched": f.schedule_matched,
                "price_status": f.price_status,
                "constraint_status": f.constraint_status,
                "score": (f.score_breakdown or {}).get("score"),
                "verification": {
                    "fare": getattr(f.verification.fare, "status", None) if f.verification else None,
                    "schedule": getattr(f.verification.schedule, "status", None) if f.verification else None,
                    "seats": getattr(f.verification.seats, "status", None) if f.verification else None,
                    "booking": getattr(f.verification.booking, "status", None) if f.verification else None,
                },
                "freshness": getattr(f.verification, "freshness_status", None) if f.verification else None,
                "risk_level": None,  # filled below from decision workspace if present
            }
            for f in (result.matched_fares or [])
        ]
        ws_risk = {}
        try:
            ws = getattr(result, "decision_workspace", None) or {}
            for k, v in (ws.get("connection_risk") or {}).items():
                ws_risk[k] = v.get("risk_level")
        except Exception:
            ws_risk = {}
        for snap, f in zip(fares_snapshot, result.matched_fares or []):
            snap["risk_level"] = ws_risk.get(f.flight_number) or ws_risk.get(f.flight_number.split("-")[0] if f.flight_number else "")

        provider_record_counts = {
            "aviationstack": len(result.flights_found),
            "ignav": len(result.fares_found),
        }
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO searches (searched_at, record_json, fares_json, schema_version) VALUES (?, ?, ?, ?)",
                (record["searched_at"], json.dumps(record, ensure_ascii=True), json.dumps(fares_snapshot, ensure_ascii=True), self.CURRENT_SCHEMA_VERSION),
            )
            for provider, status in (result.provider_statuses or {}).items():
                duration = (result.provider_durations_ms or {}).get(provider)
                connection.execute(
                    "INSERT INTO provider_events (search_id, provider, status, duration_ms, record_count, error_category, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["search_id"],
                        provider,
                        status,
                        int(duration) if duration is not None else None,
                        provider_record_counts.get(provider),
                        status if status in {"error", "malformed_response", "provider_unavailable", "unavailable", "timeout"} else None,
                        record["recorded_at"],
                    ),
                )
            connection.commit()

    def get_searches(self, limit: int = 10000) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            try:
                rows = connection.execute(
                    "SELECT record_json, fares_json FROM searches ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = [
                    {"record_json": r["record_json"], "fares_json": "[]"}
                    for r in connection.execute(
                        "SELECT record_json FROM searches ORDER BY id DESC LIMIT ?", (limit,)
                    ).fetchall()
                ]
        out = []
        for row in rows:
            rec = self._normalize_record(json.loads(row["record_json"]))
            try:
                rec["fares"] = json.loads(row["fares_json"] or "[]")
            except Exception:
                rec["fares"] = []
            out.append(rec)
        return out

    def record_interaction(self, event_type: str, session_id: str | None = None,
                           search_id: str | None = None, flight_key: str | None = None,
                           provider: str | None = None) -> None:
        """Record a real user interaction. Clicks are clicks — never bookings."""
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO interaction_events (event_id, session_id, search_id, flight_key, event_type, provider, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), session_id, search_id, flight_key, event_type, provider,
                     datetime.now(timezone.utc).isoformat()),
                )
                connection.commit()
        except Exception:
            pass  # analytics must never crash the app

    def get_interactions(self, limit: int = 10000) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            try:
                rows = connection.execute(
                    "SELECT * FROM interaction_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    _SUCCESS_STATUSES = {"success", "ok", "partial_response"}
    _FAILURE_STATUSES = {"error", "malformed_response", "provider_unavailable", "unavailable", "timeout", "future_date_unavailable"}

    def get_provider_events(self, limit: int = 10000) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            try:
                rows = connection.execute(
                    "SELECT * FROM provider_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(r) for r in rows]

    def provider_reliability(self) -> list[dict[str, Any]]:
        """Aggregate per-provider success rate, latency, and record counts."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT provider, status, duration_ms, record_count FROM provider_events"
            ).fetchall()
        aggregates: dict[str, dict[str, Any]] = {}
        for row in rows:
            provider = row["provider"]
            agg = aggregates.setdefault(provider, {
                "provider": provider,
                "total_calls": 0,
                "success_calls": 0,
                "failure_calls": 0,
                "partial_calls": 0,
                "total_duration_ms": 0,
                "duration_samples": 0,
                "total_records": 0,
            })
            agg["total_calls"] += 1
            status = row["status"] or "unknown"
            if status in self._SUCCESS_STATUSES:
                agg["success_calls"] += 1
            elif status in self._FAILURE_STATUSES:
                agg["failure_calls"] += 1
            if status == "partial_response":
                agg["partial_calls"] += 1
            if row["duration_ms"] is not None:
                agg["total_duration_ms"] += row["duration_ms"]
                agg["duration_samples"] += 1
            if row["record_count"] is not None:
                agg["total_records"] += row["record_count"]
        result = []
        for agg in aggregates.values():
            total = agg["total_calls"]
            result.append({
                "provider": agg["provider"],
                "total_calls": total,
                "success_calls": agg["success_calls"],
                "failure_calls": agg["failure_calls"],
                "partial_calls": agg["partial_calls"],
                "success_pct": round(agg["success_calls"] / total * 100, 1) if total else None,
                "failure_pct": round(agg["failure_calls"] / total * 100, 1) if total else None,
                "avg_duration_ms": round(agg["total_duration_ms"] / agg["duration_samples"], 1) if agg["duration_samples"] else None,
                "avg_records_per_call": round(agg["total_records"] / total, 2) if total else None,
            })
        return sorted(result, key=lambda x: x["provider"])