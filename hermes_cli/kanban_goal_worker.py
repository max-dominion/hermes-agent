"""Quiet CLI ownership of a native Kanban Goal worker's turn boundaries."""
from __future__ import annotations

import logging
import os

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.goals import DEFAULT_MAX_TURNS, run_kanban_goal_loop

logger = logging.getLogger(__name__)


def run_goal_query(cli, query):
    """Run one claimed Goal worker, retaining history and the last actual result.

    Only this live owner suppresses the inner terminal-only stop nudge. An env
    flag on an ordinary agent is not sufficient. Inner iteration exhaustion still
    closes the run through the native finalizer; it never buys a fresh budget here.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID", ""))
    except ValueError as exc:
        raise RuntimeError("Goal worker requires a valid claimed run id") from exc
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        status = kb.goal_run_status(conn, task_id, run_id)
    if task is None or not task.goal_mode or run_id < 1 or status != "running":
        raise RuntimeError("Goal worker no longer owns an active Goal task")

    result = {}

    def task_status():
        with kbc.connect_closing() as conn:
            status = kb.goal_run_status(conn, task_id, run_id)
        # Board transitions take precedence over transport/interrupt results.
        if status != "running":
            return status if status != "ready" else "superseded"
        if isinstance(result, dict) and (
            result.get("failed") or result.get("partial") or result.get("interrupted")
        ):
            return "turn_failed"
        if getattr(cli.agent, "_interrupt_requested", False):
            return "interrupted"
        return status

    def run_turn(prompt):
        nonlocal result
        # The judge can take time: cancellation/reclaim must win before a new call.
        if task_status() != "running":
            raise RuntimeError("Goal worker stopped before continuation")
        try:
            result = cli.agent.run_conversation(
                user_message=prompt, conversation_history=cli.conversation_history,
            )
        except Exception as exc:
            result = {"failed": True, "error": f"Goal turn failed: {type(exc).__name__}"}
            raise
        if isinstance(result, dict) and isinstance(result.get("messages"), list):
            cli.conversation_history = result["messages"]
        if getattr(cli.agent, "session_id", None):
            cli.session_id = cli.agent.session_id
        response = result.get("final_response", "") if isinstance(result, dict) else str(result)
        if response:
            print(response)
        return response or ""

    def block(reason):
        with kbc.connect_closing() as conn:
            kb.block_task(conn, task_id, reason=reason, expected_run_id=run_id)

    previous = getattr(cli.agent, "_kanban_goal_loop_active", False)
    cli.agent._kanban_goal_loop_active = True
    try:
        checkpoint = (
            "[Native Kanban Goal loop is active. Before the inner iteration cap, "
            "you may end a turn with a verified progress checkpoint; the same "
            "worker will be judged and continued within its Goal budget. This "
            "is not task completion. Preserve review, consent and cancellation "
            "gates. Do not block merely to checkpoint.]"
        )
        initial_query = (query + "\n\n" + checkpoint if isinstance(query, str)
                         else [*query, {"type": "text", "text": checkpoint}])
        first_response = run_turn(initial_query)
        outcome = run_kanban_goal_loop(
            task_id=task_id,
            goal_text="\n\n".join(p for p in (task.title, task.body) if p),
            run_turn=run_turn, task_status_fn=task_status, block_fn=block,
            max_turns=task.goal_max_turns or DEFAULT_MAX_TURNS,
            first_response=first_response, log=logger.info,
        )
        logger.info("kanban goal worker finished: %s", outcome)
        return result
    finally:
        cli.agent._kanban_goal_loop_active = previous
