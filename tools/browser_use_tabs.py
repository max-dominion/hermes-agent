"""Tabs browser_exec opens on a SHARED browser (a CDP override such as ``browser.cdp_url``), closed
when the task that opened them is cleaned up.

A launched or cloud browser is torn down whole at task cleanup (``browser_session_lifecycle``). A shared
browser is not: it belongs to the user, so teardown leaves it running. Before this module nothing
closed the tabs an agent opened there, and the harness daemon that drove them outlived the task. On
the control Mac that grew the Automation Chrome (127.0.0.1:9223) to 60 pages / 28 GiB and paused most
apps (2026-11-09).

Each call on a shared browser gets a per-task ledger file (path in ``LEDGER_ENV``). The own-tab
preamble and the ``new_tab`` wrapper (``browser_use_shot``, harness side) append the target ids they
create. ``release_task`` closes those targets, then stops the harness daemons only that task drove;
the harness closes its own dedicated tab when it stops. Tabs the task did not create are never
touched, and an auth/login page is kept so a human can finish a sign-in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

from tools.browser_harness_dirs import harness_pid_file

logger = logging.getLogger(__name__)

# Read by the harness-side helpers (browser_use_shot, the own-tab preamble), which import nothing
# from Hermes: the path crosses the process boundary in the child env, like BH_AGENT_WORKSPACE.
LEDGER_ENV = "BH_HERMES_TAB_LEDGER"
_HTTP_TIMEOUT_S = 5.0
# A page in a sign-in flow may be waiting for a human; closing it would lose that work.
_KEEP_URL_RE = re.compile(
    r"^https?://(?:[^/]+\.)?discord\.com/|/(?:log-?in|sign-?in|oauth2?|auth|authorize|sso|callback)(?:[/?#]|$)",
    re.IGNORECASE)

_lock = threading.Lock()
# daemon identity -> {task key -> CDP endpoint} for daemons driving a shared browser
_daemon_tasks: dict[tuple[str, ...], dict[str, str]] = {}


def _task_key(task_id: Optional[str]) -> str:
    return str(task_id or "default")


def ledger_path(env: dict, task_id: Optional[str]) -> Path:
    """Ledger of tabs ``task_id`` opened through the daemon ``env`` drives (beside its pid file)."""
    pid_file = harness_pid_file(env)
    digest = hashlib.sha256(_task_key(task_id).encode()).hexdigest()[:16]
    return pid_file.with_name(f"{pid_file.stem}.tabs-{digest}")


def register_call(env: dict, daemon: tuple[str, ...], task_id: Optional[str], cdp: str) -> None:
    """Mark this call as a shared-browser call of ``task_id``: export the ledger path to the harness
    and remember which task drives ``daemon``."""
    path = ledger_path(env, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    env[LEDGER_ENV] = str(path)
    with _lock:
        _daemon_tasks.setdefault(daemon, {})[_task_key(task_id)] = cdp


def _http_base(cdp: str) -> Optional[str]:
    """``http(s)://host:port`` of a CDP endpoint given as ws(s):// or http(s):// URL."""
    parsed = urllib.parse.urlparse(cdp or "")
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(parsed.scheme)
    return f"{scheme}://{parsed.netloc}" if scheme and parsed.netloc else None


def _read_ledger(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").split()
    except OSError:
        return []
    return list(dict.fromkeys(t for t in lines if re.fullmatch(r"[A-Za-z0-9]{8,64}", t)))


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def close_ledger_tabs(path: Path, cdp: str) -> dict[str, int]:
    """Close the page targets listed in ``path`` that still exist on ``cdp``; keep sign-in pages.
    Removes the ledger. Returns counts for the log."""
    counts = {"closed": 0, "kept": 0, "gone": 0}
    ids = _read_ledger(path)
    base = _http_base(cdp)
    if ids and base:
        try:
            live = {t.get("id"): t for t in _get_json(f"{base}/json/list") if t.get("type") == "page"}
        except (OSError, ValueError) as exc:
            logger.debug("browser_exec tab release: target list failed: %s", exc)
            return counts  # keep the ledger: a later release retries
        for tid in ids:
            target = live.get(tid)
            if target is None:
                counts["gone"] += 1
            elif _KEEP_URL_RE.search(target.get("url") or ""):
                counts["kept"] += 1
            else:
                try:
                    urllib.request.urlopen(f"{base}/json/close/{tid}", timeout=_HTTP_TIMEOUT_S).close()
                    counts["closed"] += 1
                except (OSError, ValueError) as exc:
                    logger.debug("browser_exec tab release: close %s failed: %s", tid, exc)
    try:
        path.unlink()
    except OSError:
        pass
    return counts


def release_daemon_tabs(daemon: tuple[str, ...], env: dict, task_id: Optional[str] = None) -> None:
    """Close the ledgered tabs of ``task_id`` (every task when None) on ``daemon``."""
    with _lock:
        tasks = dict(_daemon_tasks.get(daemon, {}))
    for task, cdp in tasks.items():
        if task_id is not None and task != _task_key(task_id):
            continue
        counts = close_ledger_tabs(ledger_path(env, task), cdp)
        if any(counts.values()):
            logger.info("browser_exec: released tabs for task %s on daemon %s: %s", task, daemon[-1], counts)
        with _lock:
            remaining = _daemon_tasks.get(daemon, {})
            remaining.pop(task, None)
            if not remaining:
                _daemon_tasks.pop(daemon, None)


def daemons_of_task(task_id: Optional[str]) -> list[tuple[str, ...]]:
    """Shared-browser daemons ``task_id`` drove."""
    key = _task_key(task_id)
    with _lock:
        return [d for d, tasks in _daemon_tasks.items() if key in tasks]


def is_daemon_idle_after(daemon: tuple[str, ...], task_id: Optional[str]) -> bool:
    """True when no task other than ``task_id`` still drives ``daemon``."""
    key = _task_key(task_id)
    with _lock:
        return not (set(_daemon_tasks.get(daemon, {})) - {key})


def forget(daemons: Iterable[tuple[str, ...]]) -> None:
    with _lock:
        for daemon in daemons:
            _daemon_tasks.pop(daemon, None)
