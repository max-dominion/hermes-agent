"""Real board + quiet CLI/stop-gate integration; model responses are scripted."""
import logging
from types import SimpleNamespace

import pytest

import cli
from agent.kanban_stop import build_kanban_stop_nudge
from agent.turn_stop_gates import _kanban_stop_nudge
from hermes_cli import goals, kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def claimed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Produce verified work", assignee="default", goal_mode=True, goal_max_turns=3)
        task = kb.claim_task(conn, tid, claimer="test:1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    return tid, task.current_run_id


def finish(tid, rid, status):
    with kbc.connect_closing() as conn:
        if status == "done":
            assert kb.complete_task(conn, tid, summary="verified", expected_run_id=rid)
        elif status == "review":
            assert kb.request_review(conn, tid, summary="verified", expected_run_id=rid)
        else:
            assert kb.block_task(conn, tid, reason="human decision", expected_run_id=rid)


@pytest.mark.parametrize("case,expected_calls,expected_status,exit_code", [
    ("done", 2, "done", 0), ("review", 2, "review", 0), ("blocked", 2, "blocked", 0),
    ("budget", 3, "blocked", 0), ("judge_done", 2, "blocked", 0),
    ("cancel_during_judge", 1, "blocked", 0),
    ("failed_first", 1, "running", 1), ("failed_second", 2, "running", kb.KANBAN_RATE_LIMIT_EXIT_CODE),
    ("inner_budget", 1, "ready", 0),
    ("exception_second", 2, "running", 1),
])
def test_goal_checkpoint_lifecycle(claimed, monkeypatch, case, expected_calls, expected_status, exit_code):
    tid, rid = claimed
    calls = []
    histories = []
    agent = SimpleNamespace(session_id="goal-session", _interrupt_requested=False)
    # The environment by itself must NOT exempt ordinary workers.
    assert build_kanban_stop_nudge(messages=[])
    assert _kanban_stop_nudge(agent, [])

    def run_conversation(*, user_message, conversation_history):
        assert agent._kanban_goal_loop_active is True
        assert _kanban_stop_nudge(agent, conversation_history) is None
        if histories:
            assert conversation_history is histories[-1]
            assert conversation_history[-1]["content"] == "checkpoint"
        calls.append(user_message)
        if case == "exception_second" and len(calls) == 2:
            raise RuntimeError("transport exception")
        messages = list(conversation_history) + [
            {"role": "user", "content": user_message}, {"role": "assistant", "content": "checkpoint"},
        ]
        histories.append(messages)
        result = {"final_response": "checkpoint", "messages": messages}
        if (case == "failed_first" and len(calls) == 1) or (case == "failed_second" and len(calls) == 2):
            result.update(failed=True, failure_reason="billing" if len(calls) == 2 else "api_error")
        if case in ("done", "review", "blocked") and len(calls) == 2:
            finish(tid, rid, case)
        if case == "inner_budget":
            from agent.turn_finalizer import _resolve_budget_fallback
            agent.max_iterations = 2
            agent.iteration_budget = SimpleNamespace(remaining=0)
            _resolve_budget_fallback(
                agent, final_response="checkpoint", api_call_count=2,
                interrupted=False, failed=False, messages=messages, _turn_exit_reason="completed",
                _pending_verification_response=None, _pending_verification_response_previewed=False,
                logger=logging.getLogger(__name__),
            )
        return result

    def judge(*args, **kwargs):
        if case == "cancel_during_judge":
            finish(tid, rid, "blocked")
            return "blocked", "stale verdict", False, None, False
        return ("done" if case == "judge_done" else "continue"), "more work", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    agent.run_conversation = run_conversation
    owner = SimpleNamespace(agent=agent, session_id=agent.session_id, conversation_history=[])
    with pytest.raises(SystemExit) as exc:
        cli._run_quiet_single_query(owner, "start")
    assert exc.value.code == exit_code
    assert len(calls) == expected_calls
    assert owner.conversation_history is histories[-1]
    assert agent._kanban_goal_loop_active is False
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == expected_status
        if case == "inner_budget":
            run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (rid,)).fetchone()
            assert run["outcome"] == "timed_out"
        if case == "cancel_during_judge":
            assert len([c for c in kb.list_comments(conn, tid) if "stale verdict" in c.body]) == 0


@pytest.mark.parametrize("invalid", ["missing", "malformed", "superseded", "classic"])
def test_goal_owner_fails_closed(claimed, monkeypatch, invalid):
    tid, rid = claimed
    if invalid == "missing":
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    elif invalid == "malformed":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-an-id")
    elif invalid == "superseded":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(rid + 1))
    else:
        with kbc.connect_closing() as conn:
            classic = kb.create_task(conn, title="Classic task", assignee="default", goal_mode=False)
            task = kb.claim_task(conn, classic, claimer="test:2")
        monkeypatch.setenv("HERMES_KANBAN_TASK", classic)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    owner = SimpleNamespace(agent=SimpleNamespace(run_conversation=lambda **kw: pytest.fail("no model call")), conversation_history=[])
    with pytest.raises(RuntimeError):
        cli._run_quiet_single_query(owner, "start")
    assert not getattr(owner.agent, "_kanban_goal_loop_active", False)


@pytest.mark.parametrize("transition", ["done", "review", "blocked", "successor", "repeat"])
def test_budget_finalizer_is_bound_to_original_run(claimed, transition):
    from agent.turn_finalizer import _record_kanban_budget_exhausted
    tid, rid = claimed
    log = logging.getLogger(__name__)
    if transition == "repeat":
        _record_kanban_budget_exhausted(tid, 2, 2, log)
    else:
        finish(tid, rid, "blocked" if transition == "successor" else transition)
    with kbc.connect_closing() as conn:
        if transition == "successor":
            assert kb.unblock_task(conn, tid)
            successor = kb.claim_task(conn, tid, claimer="test:successor")
            assert successor.current_run_id != rid
        before_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        before_runs = [dict(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))]
        before_events = [dict(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))]
    _record_kanban_budget_exhausted(tid, 2, 2, log)
    with kbc.connect_closing() as conn:
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before_task
        assert [dict(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,))] == before_runs
        assert [dict(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))] == before_events
