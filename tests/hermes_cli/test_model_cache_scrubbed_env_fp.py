"""One profile has one catalog cache key, whether or not this process's env holds the provider key.

The gateway loads the home's ``.env`` into ``os.environ``; the Hermes children it spawns (terminal
commands, cron scripts) run with provider keys scrubbed but share the same
``provider_models_cache.json``. The key used to hash ``os.environ`` values, so a child's prewarm
or refresh wrote the Anthropic row under a key the gateway never matched. The non-blocking
``/model`` picker then fell back to the curated list, which lacks the live-only
``claude-fable-5-2``, at every open after the row went stale.
"""
import json
import time

import pytest

from hermes_cli import model_switch_providers, models
from hermes_cli.model_switch_providers import list_picker_providers

LIVE = ["claude-opus-5-5", "claude-fable-5-2", "claude-fable-5-1", "claude-fable-5"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-profile-key\n", encoding="utf-8")
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": [
        {"id": "o1", "auth_type": "oauth", "source": "manual:hermes_pkce", "access_token": "tok"}]}}))
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    monkeypatch.setattr(models, "_get_model_config_dict", dict)
    monkeypatch.setattr(models, "_fetch_anthropic_models", lambda **_kw: list(LIVE))
    monkeypatch.setattr(models, "_spawn_swr_refresh", lambda *a, **k: None)
    return home


def _gateway_picker_anthropic_models():
    rows = list_picker_providers(max_models=50, non_blocking_catalogs=True, probe_custom_providers=False,
                                 current_provider="openrouter", current_model="x")
    return next(r for r in rows if r["slug"] == "anthropic")["models"]


def _age_row_past_ttl():
    cache = models._load_provider_models_cache()
    cache["anthropic"]["at"] = time.time() - models._PROVIDER_MODELS_CACHE_TTL - 600
    models._save_provider_models_cache(cache)


def test_scrubbed_child_prewarm_serves_the_gateway_picker_after_ttl(home, monkeypatch):
    assert not (home / "provider_models_cache.json").exists()  # cold cache
    # A Hermes child process: the sanitizer removed the provider key from its env.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "claude-fable-5-2" in models.cached_provider_model_ids("anthropic")  # prewarm / background fill
    _age_row_past_ttl()
    # The gateway process: load_hermes_dotenv put the same profile key in os.environ.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-profile-key")
    assert "claude-fable-5-2" in _gateway_picker_anthropic_models()


def test_tui_prewarm_thread_and_gateway_read_share_the_row(home, monkeypatch):
    from hermes_cli import inventory

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-profile-key")
    ctx = inventory.ConfigContext(current_provider="anthropic", current_model="claude-opus-5-5",
                                  current_base_url="", user_providers={}, custom_providers=[])
    monkeypatch.setattr(inventory, "load_picker_context", lambda: ctx)
    model_switch_providers._picker_prewarm_done.clear()
    try:
        model_switch_providers.prewarm_picker_cache_async().join(timeout=30)
    finally:
        model_switch_providers._picker_prewarm_done.clear()
    assert "claude-fable-5-2" in models._load_provider_models_cache()["anthropic"]["models"]
    _age_row_past_ttl()
    monkeypatch.delenv("ANTHROPIC_API_KEY")  # a scrubbed child opening the picker
    assert "claude-fable-5-2" in _gateway_picker_anthropic_models()


def test_rekeying_the_profile_still_invalidates_the_row(home, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    models.cached_provider_model_ids("anthropic")
    (home / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-other-account\n", encoding="utf-8")
    assert models.cached_provider_model_ids("anthropic", non_blocking=True) == []
