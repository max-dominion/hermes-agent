"""Explicit unblock acknowledges existing PR evidence for one claim only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    return home


def test_unblock_acknowledges_only_existing_comments_until_next_claim(board, monkeypatch):
    # Same-second operations must use ID ordering, not timestamp guesses.
    monkeypatch.setattr(kb.time, 'time', lambda: 1800000000)
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title='prerequisite')
        tid = kb.create_task(conn, title='unfinished evaluation', assignee='default')
        kb.add_comment(conn, tid, author='worker', body='Draft https://github.com/a/b/pull/1')
        assert kbd.check_respawn_guard(conn, tid) == 'active_pr'
        assert kb.block_task(conn, tid, reason='login needed', kind='needs_input')
        kb.link_tasks(conn, parent, tid)
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == 'todo'
        assert kbd.check_respawn_guard(conn, tid) is None
        assert kb.claim_task(conn, tid) is None
        assert kb.complete_task(conn, parent, summary='prerequisite verified')
        kb.recompute_ready(conn)
        kb.add_comment(conn, tid, author='operator', body='Continue within existing budget')
        assert kbd.check_respawn_guard(conn, tid) is None
        kb.add_comment(conn, tid, author='worker', body='New https://github.com/a/b/pull/2')
        assert kbd.check_respawn_guard(conn, tid) == 'active_pr'
        assert kb.block_task(conn, tid, reason='new evidence', kind='capability')
        assert kb.unblock_task(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) is None

    barrier = Barrier(2)
    def claim():
        with kbc.connect() as conn:
            barrier.wait(timeout=10)
            return kb.claim_task(conn, tid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(r is not None for r in results) == 1
    with kbc.connect() as conn:
        assert kbd.check_respawn_guard(conn, tid) == 'active_pr'
        assert kb.reclaim_task(conn, tid, reason='worker died')
        assert kbd.check_respawn_guard(conn, tid) == 'active_pr'
        assert len(kb.list_comments(conn, tid)) == 3


def test_resume_keeps_auth_cooldown_dispatch_and_legacy_fences(board, monkeypatch):
    monkeypatch.setattr(kbd, '_profile_exists_fn', lambda: lambda _: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='resumed work', assignee='default')
        other = kb.create_task(conn, title='ordinary duplicate', assignee='default')
        for task in (tid, other):
            kb.add_comment(conn, task, author='worker', body='Draft https://github.com/a/b/pull/1')
        assert kb.block_task(conn, tid, reason='operator decision', kind='needs_input')
        assert kb.unblock_task(conn, tid)
        # Error-state fixtures affect this isolated native board only.
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET last_failure_error = ? WHERE id = ?', ('401 unauthorized', tid))
        assert kbd.check_respawn_guard(conn, tid) == 'blocker_auth'
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET last_failure_error = NULL WHERE id = ?', (tid,))
        calls = []
        result = kbd.dispatch_once(conn, max_spawn=2, spawn_fn=lambda task, workspace: calls.append(task.id))
        assert calls == [tid]
        assert (other, 'active_pr') in result.respawn_guarded
        assert kb.block_task(conn, tid, reason='quota wait', kind='transient')
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET outcome = 'rate_limited' WHERE task_id = ?", (tid,))
        monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '600')
        assert kb.unblock_task(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) == 'rate_limit_cooldown'

        for delta, expected in [(1, 'active_pr'), (0, 'active_pr')]:
            legacy = kb.create_task(conn, title='legacy resume', assignee='default')
            monkeypatch.setattr(kb.time, 'time', lambda: 1800000100)
            kb.add_comment(conn, legacy, author='worker', body='https://github.com/a/b/pull/3')
            assert kb.block_task(conn, legacy, reason='legacy gate', kind='needs_input')
            monkeypatch.setattr(kb.time, 'time', lambda: 1800000100 + delta)
            assert kb.unblock_task(conn, legacy)
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET payload = NULL WHERE task_id = ? AND kind = 'unblocked'", (legacy,))
            assert kbd.check_respawn_guard(conn, legacy) == expected
            if delta == 0:
                # IDs can differ from timestamp order (timestamp before lock).
                monkeypatch.setattr(kb.time, 'time', lambda: 1800000099)
                kb.add_comment(conn, legacy, author='worker', body='delayed status')
                monkeypatch.setattr(kb.time, 'time', lambda: 1800000100)
                assert kbd.check_respawn_guard(conn, legacy) == 'active_pr'
