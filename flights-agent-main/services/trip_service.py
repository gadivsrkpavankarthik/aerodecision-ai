"""Additive SQLite persistence for saved decision artifacts."""
import json, sqlite3, uuid
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

class TripStore:
    def __init__(self, path):
        self.path=Path(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS saved_trips (trip_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL, payload_json TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_saved_trips_created ON saved_trips(created_at)')
            db.execute('INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (1, ?)',(datetime.now(timezone.utc).isoformat(),))
            version = db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
            if version < 2:
                cols = [r[1] for r in db.execute('PRAGMA table_info(saved_trips)').fetchall()]
                if 'session_id' not in cols:
                    db.execute('ALTER TABLE saved_trips ADD COLUMN session_id TEXT')
                    db.execute('CREATE INDEX IF NOT EXISTS idx_saved_trips_session ON saved_trips(session_id)')
                db.execute('INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (2, ?)',(datetime.now(timezone.utc).isoformat(),))
            db.commit()
    def save(self, payload, title='Saved trip', session_id=None):
        trip_id=uuid.uuid4().hex; created=datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO saved_trips(trip_id,created_at,title,payload_json,session_id) VALUES (?,?,?,?,?)',(trip_id,created,title,json.dumps(payload,ensure_ascii=True),session_id))
            db.commit()
        return {'trip_id':trip_id,'created_at':created,'title':title,'payload':payload}
    def list(self, limit=100, session_id=None):
        with closing(sqlite3.connect(self.path)) as db:
            if session_id is None:
                rows=db.execute('SELECT trip_id,created_at,title,payload_json,session_id FROM saved_trips ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
            else:
                rows=db.execute('SELECT trip_id,created_at,title,payload_json,session_id FROM saved_trips WHERE session_id IS NULL OR session_id=? ORDER BY created_at DESC LIMIT ?', (session_id,limit)).fetchall()
        return [self._with_age({'trip_id':r[0],'created_at':r[1],'title':r[2],'payload':json.loads(r[3]),'session_id':r[4]}) for r in rows]
    def get(self, trip_id, session_id=None):
        with closing(sqlite3.connect(self.path)) as db: row=db.execute('SELECT trip_id,created_at,title,payload_json,session_id FROM saved_trips WHERE trip_id=?',(trip_id,)).fetchone()
        if not row: return None
        if session_id is not None and row[4] is not None and row[4] != session_id: return None
        trip={'trip_id':row[0],'created_at':row[1],'title':row[2],'payload':json.loads(row[3]),'session_id':row[4]}
        return self._with_age(trip)
    def delete(self, trip_id, session_id=None):
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT session_id FROM saved_trips WHERE trip_id=?',(trip_id,)).fetchone()
            if not row: return False
            if session_id is not None and row[0] is not None and row[0] != session_id: return False
            db.execute('DELETE FROM saved_trips WHERE trip_id=?',(trip_id,))
            db.commit()
        return True
    @staticmethod
    def _with_age(trip):
        payload=trip['payload']; selected=payload.get('selected_itinerary') or {}
        stamps={'fare':selected.get('retrieved_at'),'schedule':selected.get('schedule_retrieved_at')}
        def age_status(stamp):
            status='FRESHNESS NOT ESTABLISHED'
            if stamp:
                try:
                    dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                    delta=datetime.now(timezone.utc)-dt.astimezone(timezone.utc)
                    if delta>=timedelta(0):
                        status='RECENTLY RETRIEVED' if delta<=timedelta(hours=24) else 'HISTORICAL EVIDENCE — REVALIDATION REQUIRED'
                except (ValueError,TypeError,AttributeError): pass
            return status
        per_source={source:age_status(stamp) for source,stamp in stamps.items()}
        known=[status for status in per_source.values() if status!='FRESHNESS NOT ESTABLISHED']
        status='HISTORICAL EVIDENCE — REVALIDATION REQUIRED' if 'HISTORICAL EVIDENCE — REVALIDATION REQUIRED' in known else 'RECENTLY RETRIEVED' if known else 'FRESHNESS NOT ESTABLISHED'
        stamp=stamps['fare'] or stamps['schedule']
        trip['evidence_age_status']=status
        trip['evidence_captured_at']=stamp
        trip['evidence_timestamps']=stamps
        trip['evidence_age_by_source']=per_source
        trip['original_evidence_status']=(selected.get('trust_status') or {}).get('overall') or selected.get('price_status') or 'UNKNOWN'
        trip['original_schedule_status']=(selected.get('trust_status') or {}).get('schedule') or ('SCHEDULE UNCONFIRMED' if not selected.get('schedule_matched') else 'SCHEDULE CONFIRMED')
        trip['current_validation_status']='not_revalidated'
        trip['revalidation_occurred']=False
        return trip
