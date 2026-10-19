"""Native Kanban Goal worker ownership of its turn boundaries (Dominion carry).

Upstream drives a goal_mode card through ``goals.run_kanban_goal_loop`` from three
places: the ``-Q`` and ``-q`` one-shot CLI paths (``hermes_cli/cli_single_query.py``)
and the managed gateway worker (``gateway/session_kanban.py::_run_task_turns``).
This module adds what those callers need for an actual Goal loop owner:

* ``require_goal_owner`` proves, before any model call, that this process owns an
  active Goal run (valid run id, goal_mode card, run still ``running``); it fails
  closed with ``RuntimeError`` otherwise.
* ``goal_loop_owner`` marks the agent as the live loop owner for the duration, so
  the terminal-only stop nudge (``agent/kanban_stop.py``) yields to evidence-backed
  progress checkpoints. An env flag on an ordinary agent is not sufficient.
* ``with_checkpoint_note`` tells the first turn that checkpoints are allowed.
* ``goal_status`` maps the board status as the loop must see it: board transitions
  win, a run released back to ``ready`` (budget finalizer, reclaim) is superseded,
  and a failed / interrupted latest turn stops the loop instead of re-entering the
  model.
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, Callable, Iterator, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

CHECKPOINT_NOTE = (
    "[Native Kanban Goal loop is active. Before the inner iteration cap, "
    "you may end a turn with a verified progress checkpoint; the same "
    "worker will be judged and continued within its Goal budget. This "
    "is not task completion. Preserve review, consent and cancellation "
    "gates. Do not block merely to checkpoint.]"
)


def goal_mode_requested() -> bool:
    return os.environ.get("HERMES_KANBAN_GOAL_MODE") == "1"


def require_goal_owner(task_id: Optional[str] = None, run_id: Any = None, db_path: Any = None):
    """Return the claimed goal task, or raise ``RuntimeError`` (fail closed, no model call)."""
    task_id = (task_id if task_id is not None else os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw = run_id if run_id is not None else os.environ.get("HERMES_KANBAN_RUN_ID", "")
    try:
        run_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Goal worker requires a valid claimed run id") from exc
    connect = (lambda: kbc.connect_closing(db_path)) if db_path is not None else kbc.connect_closing
    with connect() as conn:
        task = kb.get_task(conn, task_id) if task_id else None
        status = kb.goal_run_status(conn, task_id, run_id) if task is not None else None
    if task is None or not task.goal_mode or run_id < 1 or status != "running":
        raise RuntimeError("Goal worker no longer owns an active Goal task")
    return task


_ACTIVE_OWNER: dict = {}


@contextlib.contextmanager
def goal_loop_owner(agent=None, *, task_id: Optional[str] = None) -> Iterator[None]:
    """Mark the live loop owner. ``agent`` when it already exists; the ``-q`` chat path builds
    its agent lazily inside ``cli.chat``, so it binds the exact owned task id instead, which
    ``owner_active`` matches against the agent's own Kanban task."""
    previous = getattr(agent, "_kanban_goal_loop_active", False) if agent is not None else None
    if agent is not None:
        agent._kanban_goal_loop_active = True
    prior_owner = dict(_ACTIVE_OWNER)
    if task_id:
        _ACTIVE_OWNER.clear()
        _ACTIVE_OWNER["task_id"] = task_id
    try:
        yield
    finally:
        if agent is not None:
            agent._kanban_goal_loop_active = previous
        _ACTIVE_OWNER.clear()
        _ACTIVE_OWNER.update(prior_owner)


def owner_active(agent) -> bool:
    """True only for the actual Goal loop owner (agent flag, or the bound task id)."""
    if getattr(agent, "_kanban_goal_loop_active", False):
        return True
    bound = _ACTIVE_OWNER.get("task_id")
    if not bound:
        return False
    try:
        from agent.delegation_context import owned_kanban_task
        return owned_kanban_task() == bound
    except Exception:
        return False


def with_checkpoint_note(query):
    if isinstance(query, str):
        return query + "\n\n" + CHECKPOINT_NOTE
    if isinstance(query, list):
        return [*query, {"type": "text", "text": CHECKPOINT_NOTE}]
    return query


def goal_status(raw_status: Optional[str], last_result: Any = None, agent: Any = None) -> Optional[str]:
    """The status ``run_kanban_goal_loop`` should act on for this owner."""
    if raw_status != "running":
        return "superseded" if raw_status == "ready" else raw_status
    if isinstance(last_result, dict) and (
        last_result.get("failed") or last_result.get("partial") or last_result.get("interrupted")
    ):
        return "turn_failed"
    if agent is not None and getattr(agent, "_interrupt_requested", False):
        return "interrupted"
    return raw_status


def guarded_turn(status_fn: Callable[[], Optional[str]], run: Callable[[str], Any]) -> Callable[[str], Any]:
    """A continuation turn that re-checks ownership first: the judge call can take time,
    and a cancellation or reclaim must win before the model is entered again."""
    def turn(prompt: str):
        if status_fn() != "running":
            raise RuntimeError("Goal worker stopped before continuation")
        return run(prompt)
    return turn
