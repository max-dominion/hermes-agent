"""A failed title fallback must not escape cron's bounded finalizer."""

import sqlite3
import threading
import time
from types import SimpleNamespace

from cron import scheduler
from hermes_state import SessionDB


def test_real_locked_title_write_does_not_escape_finalizer(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    sid = "cron_locked_title"
    db.create_session(sid, "cron")
    blocker = sqlite3.connect(db.db_path)
    blocker.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    finished = threading.Event()
    original = db.set_session_title

    def title_write(*args, **kwargs):
        entered.set()
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(db, "set_session_title", title_write)
    monkeypatch.setattr(scheduler, "_cron_cleanup_timeout_seconds", lambda: 0.1)
    try:
        start = time.monotonic()
        scheduler._finalize_cron_session(db, SimpleNamespace(session_id=sid), "locked", "locked", sid)
        assert time.monotonic() - start < 5
        assert entered.is_set()
        assert not finished.is_set()
    finally:
        blocker.rollback()
        blocker.close()
        assert finished.wait(5), "the owned title writer must exit after lock release"
        db.close()


def test_failed_lineage_lookup_still_tries_plain_fallback_and_ends(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    sid = "cron_failed_lineage"
    db.create_session(sid, "cron")
    original = db.set_session_title
    calls = []

    def title_write(session_id, title):
        calls.append(title)
        if len(calls) == 1:
            raise ValueError("injected title error")
        return original(session_id, title)

    def bad_lineage(_base):
        raise ValueError("injected lineage error")

    monkeypatch.setattr(db, "set_session_title", title_write)
    monkeypatch.setattr(db, "get_next_title_in_lineage", bad_lineage)
    scheduler._finalize_cron_session(db, SimpleNamespace(session_id=sid), "lineage", "lineage", sid)
    with sqlite3.connect(db.db_path) as reader:
        title, ended, reason = reader.execute(
            "SELECT title, ended_at, end_reason FROM sessions WHERE id=?", (sid,)
        ).fetchone()
    assert len(calls) == 2
    assert title == calls[-1]
    assert ended is not None
    assert reason == "cron_incomplete_no_output"
