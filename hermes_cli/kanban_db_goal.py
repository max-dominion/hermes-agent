"""Existing-card Goal configuration; shared by authenticated control surfaces."""
from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db as kb


def set_goal_config(
    conn: sqlite3.Connection, task_id: str, *, goal_mode: bool | None = None,
    goal_max_turns: int | None = None, board: str | None = None,
) -> bool:
    """Persist supplied fields, preserving omitted/None values and native zero/default semantics.

    Reject invalid types/budgets and active runs under the same IMMEDIATE transaction
    as the update (no dispatcher race). Emit audit and post-commit observer only for
    changed fields. No nested transaction: observers must see durable state.
    """
    if goal_mode is not None and type(goal_mode) is not bool:
        raise ValueError("goal_mode must be a boolean")
    if goal_max_turns is not None and (
        type(goal_max_turns) is not int or not 0 <= goal_max_turns <= 2**63 - 1
    ):
        raise ValueError("goal_max_turns must be a non-negative SQLite integer (0 uses default)")
    supplied = {k: v for k, v in {
        "goal_mode": goal_mode, "goal_max_turns": goal_max_turns,
    }.items() if v is not None}
    if goal_max_turns == 0:
        supplied["goal_max_turns"] = None  # native create/from-row zero means inherited budget
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT goal_mode, goal_max_turns, status, claim_lock, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        changed = {k: v for k, v in supplied.items() if row[k] != v}
        if not changed:
            return True
        active_run = conn.execute(
            "SELECT 1 FROM task_runs WHERE id = ? AND status = 'running'",
            (row["current_run_id"],),
        ).fetchone()
        if row["status"] in {"running", "archived"} or row["claim_lock"] is not None or active_run:
            raise RuntimeError("cannot change Goal configuration on an active/claimed or archived task")
        sets = ", ".join(f"{field} = ?" for field in changed)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*changed.values(), task_id))
        kb._append_event(conn, task_id, "goal_config_set", changed)
    kb.notify_task_updated(conn, task_id, changed, board=board)
    return True
