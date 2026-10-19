"""Dominion: the managed (gateway-owned) Goal worker owns its stop gate and continues with history."""
import json
import os

from agent.turn_stop_gates import _kanban_stop_nudge
from gateway import session_kanban
from hermes_cli import goals, kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


class _Agent:
    session_id = "s1"

    def __init__(self, on_turn):
        self.calls, self._on_turn = [], on_turn
        self._session_db = self
        self._interrupt_requested = False

    def get_messages_as_conversation(self, _sid):
        return [{"role": "user", "content": "from-store"}]

    def run_conversation(self, message, conversation_history=None):
        self.calls.append(message)
        # The live loop owner must suppress the terminal-only stop nudge.
        assert self._kanban_goal_loop_active is True
        assert _kanban_stop_nudge(self, []) is None
        return self._on_turn(len(self.calls))


def _frame(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt", lambda *a, **k: ("", [], []))
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="goal", assignee="default", goal_mode=True, goal_max_turns=3)
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound", {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    context = {"db": str(path), "task_id": task_id, "run_id": task.current_run_id, "claim_lock": task.claim_lock,
               "skills": [], "context": "", "goal_mode": True, "goal_text": "g", "goal_max_turns": 3}
    return path, context, {"text": "work", "policy": {"kanban_json": json.dumps(context)}}


def test_managed_goal_worker_checkpoints_then_completes(tmp_path, monkeypatch):
    path, context, frame = _frame(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))

    def on_turn(n):
        if n == 2:
            with connect_closing(path) as conn:
                assert kb.complete_task(conn, context["task_id"], summary="ok", expected_run_id=context["run_id"])
        return {"completed": True, "final_response": f"checkpoint {n}", "messages": []}

    agent = _Agent(on_turn)
    result = session_kanban.run_worker_turns(agent, frame, [])
    assert len(agent.calls) == 2
    assert "Native Kanban Goal loop is active" in agent.calls[0]
    assert result["final_response"] == "checkpoint 2"
    assert agent._kanban_goal_loop_active is False
    with connect_closing(path) as conn:
        assert kb.get_task(conn, context["task_id"]).status == "done"


def test_managed_goal_worker_stops_after_failed_continuation(tmp_path, monkeypatch):
    path, context, frame = _frame(tmp_path, monkeypatch)
    monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "more", False, None, False))
    agent = _Agent(lambda n: ({"completed": True, "final_response": "c1", "messages": []} if n == 1 else
                              {"failed": True, "failure_reason": "billing", "error": "402"}))
    session_kanban.run_worker_turns(agent, frame, [])
    assert len(agent.calls) == 2  # never re-enters the model after a failed turn
