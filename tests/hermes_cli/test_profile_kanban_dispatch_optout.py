"""Chat-only profiles (profile.yaml ``kanban_dispatch: false``) never take Kanban work."""
import sqlite3

import pytest


@pytest.fixture
def hermes_root(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: root, raising=False)
    monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: root / "profiles", raising=False)
    return root


def _make_profile(root, name, meta):
    d = root / "profiles" / name
    d.mkdir()
    (d / "config.yaml").write_text("model: {}\n")
    (d / "profile.yaml").write_text(meta)
    return d


def test_flag_parsing(hermes_root):
    from hermes_cli.profiles import profile_kanban_dispatch_enabled as enabled

    _make_profile(hermes_root, "chatonly", "description: x\nkanban_dispatch: false\n")
    _make_profile(hermes_root, "strflag", "kanban_dispatch: 'off'\n")
    _make_profile(hermes_root, "worker", "description: y\n")
    assert enabled("default") is True
    assert enabled("chatonly") is False
    assert enabled("strflag") is False
    assert enabled("worker") is True
    assert enabled("missing") is True  # no metadata -> legacy behavior


def test_dispatcher_treats_chat_only_as_nonspawnable(hermes_root):
    from hermes_cli import kanban_db_dispatch as kd

    _make_profile(hermes_root, "chatonly", "kanban_dispatch: false\n")
    _make_profile(hermes_root, "worker", "description: y\n")
    fn = kd._profile_exists_fn()
    assert fn is not None
    assert fn("default") is True
    assert fn("worker") is True
    assert fn("chatonly") is False
    assert fn("nonexistent") is False

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT, claim_lock TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t1','ready','chatonly',NULL)")
    assert kd._has_spawnable(conn, "ready") is False
    conn.execute("INSERT INTO tasks VALUES ('t2','ready','worker',NULL)")
    assert kd._has_spawnable(conn, "ready") is True
