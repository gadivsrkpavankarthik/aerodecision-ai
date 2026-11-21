"""Trip ownership / session isolation tests."""
import tempfile
from pathlib import Path

from services.trip_service import TripStore


def test_session_scoped_save_list_get_delete():
    with tempfile.TemporaryDirectory() as td:
        store = TripStore(Path(td) / "t.sqlite3")
        mine = store.save({"x": 1}, "My trip", session_id="sess-a")
        theirs = store.save({"x": 2}, "Their trip", session_id="sess-b")
        legacy = store.save({"x": 3}, "Legacy trip")

        # sess-a sees own + legacy, not sess-b
        ids = {t["trip_id"] for t in store.list(session_id="sess-a")}
        assert mine["trip_id"] in ids and legacy["trip_id"] in ids
        assert theirs["trip_id"] not in ids

        # cross-session get denied
        assert store.get(theirs["trip_id"], session_id="sess-a") is None
        assert store.get(theirs["trip_id"], session_id="sess-b") is not None

        # cross-session delete denied
        assert store.delete(theirs["trip_id"], session_id="sess-a") is False
        assert store.get(theirs["trip_id"], session_id="sess-b") is not None
        assert store.delete(theirs["trip_id"], session_id="sess-b") is True
        assert store.get(theirs["trip_id"], session_id="sess-b") is None
