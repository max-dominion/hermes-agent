"""The model-catalog disk cache key follows the provider's own credentials, not every auth.json write.

The ``/model`` picker reads catalogs non-blocking: a cache row whose credential fingerprint no longer
matches is ignored and the curated static list is shown until a background refresh lands. The key
used to hash the whole ``auth.json`` file signature, so ANY write to it (an OAuth refresh, a pool
request counter, another provider's login) dropped every API-key provider's live catalog from the
next picker open. On the Discord gateway that hid ``claude-fable-5-2`` (live-only) after a restart.
"""
import json

import pytest

from hermes_cli import models
from hermes_cli.model_switch_providers import list_picker_providers
from hermes_cli.models_cache_fingerprint import auth_store_identity

LIVE_ONLY = "claude-live-only-test-9"


def _write_auth(home, *, codex_requests=0, anthropic_key="sk-ant-api03-test-key", oauth_token="tok-1"):
    store = {
        "version": 1,
        "credential_pool": {
            "anthropic": [{"id": "a1", "auth_type": "api_key", "source": "manual", "label": "key",
                           "access_token": anthropic_key, "request_count": 0}],
            "openai-codex": [{"id": "c1", "auth_type": "oauth", "source": "device_code",
                              "access_token": oauth_token, "request_count": codex_requests}],
        },
    }
    (home / "auth.json").write_text(json.dumps(store), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test-key")
    monkeypatch.setattr(models, "_spawn_swr_refresh", lambda *a, **k: None)  # no background fetch
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", dict)
    _write_auth(home)
    return home


def _anthropic_picker_models():
    rows = list_picker_providers(max_models=50, non_blocking_catalogs=True, probe_custom_providers=False,
                                 current_provider="openrouter", current_model="x")
    row = next(r for r in rows if r["slug"] == "anthropic")
    return row["models"]


def _seed_live_row():
    fp = models._credential_fingerprint("anthropic")
    models._store_cache_entry("anthropic", models._cache_entry(fp, ["claude-opus-5", LIVE_ONLY]))


def test_unrelated_auth_json_writes_keep_the_live_catalog(home):
    _seed_live_row()
    assert LIVE_ONLY in _anthropic_picker_models()
    # Routine writes by other credentials: a counter bump, then an OAuth token rotation.
    _write_auth(home, codex_requests=7)
    _write_auth(home, codex_requests=8, oauth_token="tok-2")
    assert LIVE_ONLY in _anthropic_picker_models()


def test_rekeying_the_provider_still_invalidates_its_row(home):
    _seed_live_row()
    _write_auth(home, anthropic_key="sk-ant-api03-other-account")
    assert LIVE_ONLY not in _anthropic_picker_models()


def test_identity_ignores_rotating_fields_but_not_membership(tmp_path):
    auth = tmp_path / "auth.json"
    assert auth_store_identity(auth, "anthropic") == "missing"
    _write_auth(tmp_path)
    base = auth_store_identity(auth, "anthropic")
    _write_auth(tmp_path, codex_requests=3, oauth_token="rotated")
    assert auth_store_identity(auth, "anthropic") == base
    assert auth_store_identity(auth, "openai-codex") != base
    store = json.loads(auth.read_text())
    store["credential_pool"]["anthropic"].append({"id": "a2", "auth_type": "oauth", "source": "claude_code"})
    auth.write_text(json.dumps(store))
    assert auth_store_identity(auth, "anthropic") != base


def test_anthropic_catalog_lists_a_dotted_curated_alias_once(monkeypatch):
    monkeypatch.setattr(models, "_fetch_anthropic_models",
                        lambda **_kw: ["claude-fable-5", "claude-fable-5-1", "claude-fable-5-2"])
    monkeypatch.setitem(models._PROVIDER_MODELS, "anthropic", ["claude-fable-5.1", "claude-fable-5"])
    monkeypatch.setattr(models, "_get_model_config_dict", dict)
    catalog = models._anthropic_catalog("anthropic", False)
    wire = [m.replace(".", "-") for m in catalog]
    assert len(wire) == len(set(wire)), catalog
    assert catalog[:2] == ["claude-fable-5-1", "claude-fable-5"]  # curated order, wire ids
    assert "claude-fable-5-2" in catalog
