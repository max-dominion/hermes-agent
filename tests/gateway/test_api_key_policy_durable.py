"""Dominion: a scoped API key's policy survives the durable admission path (by name, fail closed)."""
import pytest

from gateway.platforms import api_server_key_policy as kp
from gateway.session_api_turn import admit_api_turn, prepare_api_execution
from hermes_state_runtime import RuntimeStoreError

KEY = "v" * 40


def _bind_policy(api, monkeypatch):
    monkeypatch.setenv("TEST_VOICE_KEY", KEY)
    api._key_policies = kp.parse_key_policies([
        {"name": "voice", "key_env": "TEST_VOICE_KEY", "allowed_tools": ["read_file"]}])
    return api._key_policies[0]


def test_admission_carries_policy_name_never_key(api, owner, monkeypatch):
    policy = _bind_policy(api, monkeypatch)
    token = kp._api_request_key_policy.set(policy)
    try:
        _, ref, row = admit_api_turn(api, session_id="scoped", user_message="hi", conversation_history=[])
    finally:
        kp._api_request_key_policy.reset(token)
    assert row["payload"]["api_turn_v1"]["key_policy"] == "voice"
    assert KEY not in repr(row["payload"])
    prepared = prepare_api_execution(owner, ref, row["payload"])
    assert prepared["key_policy"] is policy


def test_full_key_admission_has_no_policy(api, owner, monkeypatch):
    _bind_policy(api, monkeypatch)
    _, ref, row = admit_api_turn(api, session_id="full", user_message="hi", conversation_history=[])
    assert "key_policy" not in row["payload"]["api_turn_v1"]
    assert prepare_api_execution(owner, ref, row["payload"])["key_policy"] is None


def test_removed_policy_fails_closed_at_execution(api, owner, monkeypatch):
    policy = _bind_policy(api, monkeypatch)
    token = kp._api_request_key_policy.set(policy)
    try:
        _, ref, row = admit_api_turn(api, session_id="gone", user_message="hi", conversation_history=[])
    finally:
        kp._api_request_key_policy.reset(token)
    api._key_policies = []  # config changed / key rotated away before the turn ran
    with pytest.raises(RuntimeStoreError):
        prepare_api_execution(owner, ref, row["payload"])
