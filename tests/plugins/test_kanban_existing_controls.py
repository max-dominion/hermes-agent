"""Real ASGI controls and SQLite, with native dashboard authentication."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_cli.kanban_db_goal import set_goal_config


@pytest.fixture
def controls(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    # Real dashboard imports/mounts the bundled plugin; no substituted router/auth.
    from hermes_cli import web_server
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "isolated-test-token")
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    client = TestClient(web_server.app, base_url="http://localhost")
    try:
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="legacy", assignee="default")
            yield client, conn, tid
    finally:
        client.close()


AUTH = {"Authorization": "Bearer isolated-test-token", "Origin": "http://localhost"}


def test_goal_controls_persist_validate_protect_and_audit(controls, monkeypatch):
    client, conn, tid = controls
    url = f"/api/plugins/kanban/tasks/{tid}?board=default"
    observed = []
    def observer(c, task_id, fields, **kwargs):
        # Independent reader proves the callback is post-commit.
        with kbc.connect() as reader:
            observed.append((kb.get_task(reader, task_id), set(fields)))
    monkeypatch.setattr(kb, "notify_task_updated", observer)
    assert client.patch(url, json={"goal_mode": True}).status_code == 401
    for origin in ("https://evil.example", "http://localhost:9999", "http://[", "null"):
        assert client.patch(url, json={"goal_mode": True}, headers={**AUTH, "Origin": origin}).status_code == 403
    response = client.patch(url, json={"goal_mode": True, "goal_max_turns": 31}, headers=AUTH)
    assert response.status_code == 200, response.text
    persisted = client.get(url, headers=AUTH).json()["task"]
    assert persisted["goal_mode"] is True and persisted["goal_max_turns"] == 31
    assert observed[-1][0].goal_max_turns == 31
    assert observed[-1][1] == {"goal_mode", "goal_max_turns"}
    event_count = len(kb.list_events(conn, tid))
    assert client.patch(url, json={"goal_mode": True, "goal_max_turns": 31}, headers=AUTH).status_code == 200
    assert len(kb.list_events(conn, tid)) == event_count
    assert len(observed) == 1
    event = next(e for e in kb.list_events(conn, tid) if e.kind == "goal_config_set")
    assert event.payload == {"goal_mode": True, "goal_max_turns": 31}
    for body in ({}, {"goal_mode": None}, {"title": "renamed"}):
        assert client.patch(url, json=body, headers=AUTH).status_code == 200
        assert kb.get_task(conn, tid).goal_max_turns == 31
    for body in ({"goal_mode": "true"}, {"goal_mode": 1}, {"goal_max_turns": True},
                 {"goal_max_turns": -1}, {"goal_max_turns": 1.5}, {"goal_max_turns": "20"},
                 {"goal_max_turns": 2**63}):
        assert client.patch(url, json=body, headers=AUTH).status_code == 422
    assert client.patch(url, json={"goal_mode": False, "status": "ready"}, headers=AUTH).status_code == 400
    assert client.patch(url, json={"goal_max_turns": 0}, headers=AUTH).status_code == 200
    assert kb.get_task(conn, tid).goal_max_turns is None
    assert kb.claim_task(conn, tid, claimer="isolated-test")
    assert client.patch(url, json={"goal_mode": False}, headers=AUTH).status_code == 409
    assert kb.get_task(conn, tid).goal_mode is True
    with pytest.raises(RuntimeError):
        set_goal_config(conn, tid, goal_max_turns=2)
    with pytest.raises(ValueError):
        set_goal_config(conn, tid, goal_max_turns=True)
    # Native delegated-child guard is still authoritative for real persistence.
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    with pytest.raises(PermissionError):
        set_goal_config(conn, tid, goal_mode=False)


@pytest.mark.parametrize("platform,chat,thread,ctype", [
    ("discord", "1548107738259660820", "1548107738259660820", "thread"),
    ("telegram", "-1001234567890", "39605", "group"),
])
def test_subscription_exact_readback_fence_and_idempotence(controls, platform, chat, thread, ctype):
    client, conn, tid = controls
    url = f"/api/plugins/kanban/tasks/{tid}/notify-subscription"
    body = dict(platform=platform, chat_id=chat, thread_id=thread, user_id="test-user",
                chat_type=ctype, notifier_profile="default", delivery_mode="wake")
    key = dict(task_id=tid, platform=platform, chat_id=chat, thread_id=thread)
    assert client.put(url, json=body).status_code == 401
    assert client.put(url, json=body, headers={**AUTH, "Origin": "null"}).status_code == 403
    assert client.put(url, json={**body, "delivery_mode": "typo"}, headers=AUTH).status_code == 422
    assert client.put(url, json={**body, "chat_type": "typo"}, headers=AUTH).status_code == 422
    assert kb.complete_task(conn, tid, summary="before binding")
    response = client.put(url, json=body, headers=AUTH)
    assert response.status_code == 200, response.text
    binding = response.json()["subscription"]
    for field, value in body.items():
        assert binding[field] == value
    params = dict(platform=platform, chat_id=chat, thread_id=thread, board="default")
    assert client.get(url, params=params, headers=AUTH).json()["subscription"] == binding
    assert not kbn.unseen_events_for_sub(conn, **key)[1]  # new binding fences old completion
    # Real terminal lifecycle event after binding remains pending across rebind.
    assert kb.archive_task(conn, tid)
    assert client.put(url, json=body, headers=AUTH).json()["subscription"] == binding
    pending = kbn.unseen_events_for_sub(conn, **key)[1]
    assert any(e.kind == "archived" for e in pending)
    _, cursor, claimed = kbn.claim_unseen_events_for_sub(conn, **key)
    assert claimed
    response = client.put(url, json=body, headers=AUTH)
    assert response.json()["subscription"]["last_event_id"] == cursor
    assert not kbn.claim_unseen_events_for_sub(conn, **key)[2]
    for field in ("user_id", "notifier_profile"):
        assert client.put(url, json={**body, field: "other"}, headers=AUTH).status_code == 409
    assert client.get(url, params=params, headers=AUTH).json()["subscription"]["user_id"] == body["user_id"]
    assert client.put(url, json={**body, "delivery_mode": "notify+wake"}, headers=AUTH).status_code == 200
    assert client.get(url, params=params, headers=AUTH).json()["subscription"]["delivery_mode"] == "notify+wake"
