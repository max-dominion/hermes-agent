"""The ``/model`` picker on a standalone gateway after the process-wide secret guard is armed.

A standalone gateway (``multiplex_profiles`` off) binds the launch profile's scope around each
message handler (``_standalone_scoped``), so the ``/model`` listing runs scoped. The picker's
selection callback does not: the adapter fires it later from its own interaction task. With the
guard armed (a hosted room or another profile home activated ``activate_multi_profile_hosting``),
the unscoped credential read raised ``UnscopedSecretError`` and the pick failed with "could not
read this profile's ANTHROPIC_TOKEN". The callback must re-enter the launch profile's scope, the
same one the turn binds; the guard itself stays armed.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agent import secret_scope
from agent.secret_scope import UnscopedSecretError, get_secret
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tui_gateway import launch_profile_policy


class _CapturingPickerAdapter:
    def __init__(self):
        self.kwargs = None

    async def send_model_picker(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(success=True)


@pytest.fixture
def standalone_runner(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-launch-key\n", encoding="utf-8")
    (launch / "config.yaml").write_text(
        "model:\n  default: gpt-5.4\n  provider: openrouter\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(gateway_run, "_hermes_home", launch)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    monkeypatch.setattr(
        "hermes_cli.model_switch_providers.list_picker_providers",
        lambda **_kw: [{"slug": "anthropic", "name": "Anthropic", "is_current": False,
                        "models": ["claude-fable-5-2"], "total_models": 1}])

    runner = object.__new__(GatewayRunner)
    adapter = _CapturingPickerAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner._thread_metadata_for_source = lambda *_a, **_k: None
    runner._reply_anchor_for_event = lambda *_a, **_k: None
    committed = {}

    async def _commit(result, ctx, *, source, picker):
        committed.update(model=result.new_model, provider=result.target_provider,
                         has_key=bool(result.api_key), picker=picker)
        return f"switched to {result.new_model}"

    runner._commit_model_switch = _commit
    return runner, adapter, committed


def _event():
    return MessageEvent(
        text="/model", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="1557422289354490007", chat_type="thread"))


def test_picker_selection_binds_launch_scope_after_guard_armed(standalone_runner):
    runner, adapter, committed = standalone_runner
    launch_profile_policy.activate_multi_profile_hosting()  # a second profile home is hosted
    event = _event()

    async def _scenario():
        # The message handler path the adapter calls for a slash command (scoped at entry).
        handler = runner._standalone_scoped(runner._handle_model_command)
        assert await handler(event) is None
        on_selected = adapter.kwargs["on_model_selected"]
        # The interaction callback runs outside any scope, as discord.py's View task does.
        with pytest.raises(UnscopedSecretError):
            get_secret("ANTHROPIC_API_KEY")
        return await on_selected(event.source.chat_id, "claude-fable-5-2", "anthropic")

    reply = asyncio.run(_scenario())
    assert "could not read" not in reply, reply
    assert reply == "switched to claude-fable-5-2"
    assert committed == {"model": "claude-fable-5-2", "provider": "anthropic", "has_key": True,
                         "picker": True}
    with pytest.raises(UnscopedSecretError):  # the guard is not weakened
        get_secret("ANTHROPIC_API_KEY")


def test_picker_selection_unarmed_single_profile_host_still_works(standalone_runner):
    runner, adapter, committed = standalone_runner
    event = _event()

    async def _scenario():
        assert await runner._standalone_scoped(runner._handle_model_command)(event) is None
        return await adapter.kwargs["on_model_selected"](event.source.chat_id, "claude-fable-5-2", "anthropic")

    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    assert asyncio.run(_scenario()) == "switched to claude-fable-5-2"
    assert committed["provider"] == "anthropic" and committed["has_key"]
