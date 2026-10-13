"""Scoped API keys for the API server (code-enforced tool allowlists).

A *key policy* is an extra bearer key, besides ``API_SERVER_KEY``, that unlocks only a
narrow slice of the API server: a fixed set of paths, an exact tool allowlist, a turn
limit, and no background memory/skill review. It exists for untrusted front ends such as
a phone-call bridge, where the transcript is caller speech and must never reach write or
send tools no matter what the model is talked into.

Config (``platforms.api_server.extra.key_policies`` in the profile's config.yaml)::

    key_policies:
      - name: voice
        key_env: API_SERVER_VOICE_KEY          # secret lives in the profile .env
        paths: ["/v1/chat/completions"]          # default
        allowed_tools: [mcp__missive_calendar__list_events, read_file]
        max_iterations: 8                        # default 8
        session_headers: false                   # reject X-Hermes-Session-* (default)

Enforcement points (all fail closed):
  * auth: a matching scoped key is accepted only on ``paths``; anything else is 403.
  * agent build: ``agent.tools`` / ``valid_tool_names`` are cut to ``allowed_tools``
    after every other loader ran, so tool_search bridges, memory-provider and
    context-engine tools are gone unless listed by exact name. Calls to any other tool
    name hit the normal unknown-tool path and never execute.
  * a per-thread pre_tool_call whitelist blocks anything outside the list as a second net.
"""
from __future__ import annotations

import hmac
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

MIN_KEY_LEN = 32
DEFAULT_PATHS = ("/v1/chat/completions",)
DEFAULT_MAX_ITERATIONS = 8

_api_request_key_policy: ContextVar[Optional["KeyPolicy"]] = ContextVar(
    "api_server_request_key_policy", default=None)


@dataclass(frozen=True)
class KeyPolicy:
    name: str
    key: str = field(repr=False)
    paths: Tuple[str, ...]
    allowed_tools: Tuple[str, ...]
    max_iterations: int
    session_headers: bool

    def allows_path(self, path: str) -> bool:
        return path in self.paths

    def matches(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.key.encode())


def _secret(env_name: str) -> str:
    try:
        from agent.secret_scope import get_secret
        val = get_secret(env_name, "") or ""
    except Exception:
        val = ""
    return val or os.getenv(env_name, "") or ""


def parse_key_policies(raw: Any) -> List[KeyPolicy]:
    """Validate ``key_policies``; invalid entries are dropped with a warning (never raise,
    never log key material). A policy whose key is missing or short is dropped."""
    if not raw:
        return []
    if not isinstance(raw, list):
        logger.warning("api_server key_policies ignored: expected a list")
        return []
    out: List[KeyPolicy] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        key_env = str(entry.get("key_env") or "").strip()
        tools = entry.get("allowed_tools")
        if not name or not key_env or not isinstance(tools, list):
            logger.warning("api_server key_policies: dropping invalid entry %r", name or "?")
            continue
        key = _secret(key_env)
        if len(key) < MIN_KEY_LEN:
            logger.warning("api_server key_policies: policy %r has no usable key in %s; dropped",
                           name, key_env)
            continue
        paths = entry.get("paths") or list(DEFAULT_PATHS)
        try:
            max_iter = int(entry.get("max_iterations", DEFAULT_MAX_ITERATIONS))
        except (TypeError, ValueError):
            max_iter = DEFAULT_MAX_ITERATIONS
        out.append(KeyPolicy(
            name=name, key=key,
            paths=tuple(str(p) for p in paths if isinstance(p, str)),
            allowed_tools=tuple(sorted({str(t) for t in tools if isinstance(t, str) and t})),
            max_iterations=max(1, min(max_iter, 50)),
            session_headers=bool(entry.get("session_headers", False))))
    return out


def match_policy(policies: Iterable[KeyPolicy], token: str) -> Optional[KeyPolicy]:
    hit = None
    for p in policies:
        if p.matches(token):  # no early exit: constant work per policy
            hit = hit or p
    return hit


def apply_policy_to_agent(agent: Any, policy: KeyPolicy) -> None:
    """Cut an already-built agent down to the policy. Uses the raw (pre tool-search)
    definitions so a deferred MCP tool on the allowlist is loaded directly."""
    import model_tools
    allowed = set(policy.allowed_tools)
    raw = model_tools.get_tool_definitions(
        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
        quiet_mode=True, skip_tool_search_assembly=True) or []
    by_name = {}
    for td in list(raw) + list(getattr(agent, "tools", None) or []):
        name = (td.get("function") or {}).get("name") if isinstance(td, dict) else None
        if name in allowed and name not in by_name:
            by_name[name] = td
    agent.tools = [by_name[n] for n in sorted(by_name)]
    agent.valid_tool_names = set(by_name)
    agent.max_iterations = min(getattr(agent, "max_iterations", policy.max_iterations),
                               policy.max_iterations)
    try:
        from agent.iteration_budget import IterationBudget
        agent.iteration_budget = IterationBudget(agent.max_iterations)
    except Exception:
        logger.warning("key policy %r: could not reset iteration budget", policy.name)
    agent.skip_background_review = True
    agent._hermes_key_policy = policy.name
    agent._hermes_key_policy_tools = frozenset(allowed)
    agent._skip_mcp_refresh = True
    missing = allowed - set(by_name)
    if missing:
        logger.info("key policy %r: %d allowlisted tool(s) not loaded in this profile: %s",
                    policy.name, len(missing), ", ".join(sorted(missing)))


class policy_tool_whitelist:
    """Context manager: per-thread pre_tool_call whitelist for the duration of a turn."""

    def __init__(self, policy: Optional[KeyPolicy]):
        self.policy = policy

    def __enter__(self):
        if self.policy is not None:
            from hermes_cli.plugins import set_thread_tool_whitelist
            set_thread_tool_whitelist(
                set(self.policy.allowed_tools),
                deny_msg_fmt="Tool '{tool_name}' is not allowed on this key ("
                + self.policy.name + " policy).")
        return self

    def __exit__(self, *exc):
        if self.policy is not None:
            from hermes_cli.plugins import clear_thread_tool_whitelist
            clear_thread_tool_whitelist()
        return False
