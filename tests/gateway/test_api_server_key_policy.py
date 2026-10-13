"""Scoped API-server keys: auth scoping and tool allowlist enforcement."""
from types import SimpleNamespace

import pytest

from gateway.platforms import api_server_key_policy as kp

KEY = "v" * 40


def _policies(monkeypatch, **over):
    monkeypatch.setenv("TEST_VOICE_KEY", KEY)
    entry = {"name": "voice", "key_env": "TEST_VOICE_KEY",
             "allowed_tools": ["read_file", "mcp__cal__list_events"]}
    entry.update(over)
    return kp.parse_key_policies([entry])


def test_parse_drops_short_or_missing_keys(monkeypatch):
    monkeypatch.setenv("SHORT_KEY", "abc")
    assert kp.parse_key_policies([{"name": "x", "key_env": "SHORT_KEY", "allowed_tools": []}]) == []
    assert kp.parse_key_policies([{"name": "x", "key_env": "NOPE_KEY", "allowed_tools": []}]) == []
    assert kp.parse_key_policies("bad") == []
    assert kp.parse_key_policies([{"name": "x", "key_env": "TEST"}]) == []


def test_parse_defaults(monkeypatch):
    [p] = _policies(monkeypatch)
    assert p.paths == ("/v1/chat/completions",)
    assert p.max_iterations == 8
    assert p.session_headers is False
    assert p.allowed_tools == ("mcp__cal__list_events", "read_file")
    assert KEY not in repr(p)


def test_match(monkeypatch):
    pols = _policies(monkeypatch)
    assert kp.match_policy(pols, KEY).name == "voice"
    assert kp.match_policy(pols, KEY + "x") is None
    assert kp.match_policy(pols, "") is None


def _td(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def test_apply_policy_cuts_tools(monkeypatch):
    [p] = _policies(monkeypatch, max_iterations=5)
    import model_tools
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **kw: [
        _td("read_file"), _td("terminal"), _td("write_file"), _td("mcp__cal__list_events")])
    agent = SimpleNamespace(tools=[_td("tool_search"), _td("memory")],
                            valid_tool_names={"tool_search", "memory"},
                            enabled_toolsets=["file"], disabled_toolsets=None, max_iterations=90,
                            skip_background_review=False)
    kp.apply_policy_to_agent(agent, p)
    assert agent.valid_tool_names == {"read_file", "mcp__cal__list_events"}
    assert [t["function"]["name"] for t in agent.tools] == ["mcp__cal__list_events", "read_file"]
    assert agent.max_iterations == 5
    assert agent.iteration_budget.max_total == 5
    assert agent.skip_background_review is True
    assert agent._hermes_key_policy_tools == frozenset({"read_file", "mcp__cal__list_events"})


def test_dispatch_blocks_tool_outside_policy():
    """The executor-level net: a call to a non-allowlisted tool never runs."""
    from agent import tool_executor as te
    ran = []
    agent = SimpleNamespace(_hermes_key_policy_tools=frozenset({"read_file"}))
    ref = SimpleNamespace(name="terminal", args={}, emit_post=lambda *a, **k: None)
    state = SimpleNamespace(args={}, blocked=False)
    out = te._dispatch_authorized_once(
        agent, state, ref, execute=lambda a: ran.append(a), scope_block=None,
        display_index=None, begin_execution=None, authorization_gate=None)
    assert ran == []
    assert state.blocked is True
    assert "not allowed on this key" in out


def test_tool_refresh_never_widens_policy():
    from tools import mcp_tool_agent as mta
    agent = SimpleNamespace(tools=[_td("read_file")], valid_tool_names={"read_file"},
                            _hermes_key_policy_tools=frozenset({"read_file"}),
                            _tool_snapshot_generation=0, _context_engine_tool_names=set())
    added = mta._publish_tool_snapshot(
        agent, [_td("read_file"), _td("terminal"), _td("mcp__x__send")],
        {"read_file", "terminal", "mcp__x__send"}, snapshot_generation=1,
        staged_engine_names=set(), content_aware=False, prefix_registered=None)
    assert not added
    assert agent.valid_tool_names == {"read_file"}


@pytest.mark.asyncio
async def test_check_auth_scopes_paths(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    monkeypatch.setenv("TEST_VOICE_KEY", KEY)
    cfg = PlatformConfig(enabled=True, extra={"key": "m" * 40, "key_policies": [
        {"name": "voice", "key_env": "TEST_VOICE_KEY", "allowed_tools": ["read_file"]}]})
    ad = APIServerAdapter(cfg)
    hdr = {"Authorization": f"Bearer {KEY}"}
    ok = ad._check_auth(make_mocked_request("POST", "/v1/chat/completions", headers=hdr))
    assert ok is None
    assert kp._api_request_key_policy.get().name == "voice"
    denied = ad._check_auth(make_mocked_request("GET", "/api/sessions", headers=hdr))
    assert denied.status == 403
    sess = ad._check_auth(make_mocked_request(
        "POST", "/v1/chat/completions", headers={**hdr, "X-Hermes-Session-Id": "abc"}))
    assert sess.status == 403
    bad = ad._check_auth(make_mocked_request(
        "POST", "/v1/chat/completions", headers={"Authorization": "Bearer " + "z" * 40}))
    assert bad.status == 401
    full = ad._check_auth(make_mocked_request(
        "GET", "/api/sessions", headers={"Authorization": "Bearer " + "m" * 40}))
    assert full is None
    assert kp._api_request_key_policy.get() is None
