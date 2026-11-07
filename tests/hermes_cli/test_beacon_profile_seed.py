"""Carried Beacon seeding uses stable config I/O without creating live profiles."""
from hermes_cli import profiles
from hermes_cli.config import atomic_config_write, read_user_config_raw


def test_beacon_seed_merges_once_and_preserves_unrelated_config(tmp_path, monkeypatch):
    root = tmp_path / "root"
    child = tmp_path / "child"
    root.mkdir()
    child.mkdir()
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    beacon = {"command": "/fixture/beacon-hooks start", "timeout": 10}
    other = {"command": "/fixture/other-hook"}
    atomic_config_write(root / "config.yaml", {
        "hooks": {"on_session_start": [beacon, other]},
    })
    atomic_config_write(child / "config.yaml", {
        "model": {"default": "fixture-model", "provider": "fixture-provider"},
        "hooks": {"on_session_start": [other]},
    })
    profiles._seed_beacon_hooks(child)
    profiles._seed_beacon_hooks(child)
    actual = read_user_config_raw(child / "config.yaml")
    assert actual["hooks"]["on_session_start"] == [other, beacon]
    assert actual["hooks_auto_accept"] is True
    assert actual["model"] == {"default": "fixture-model", "provider": "fixture-provider"}


def test_beacon_seed_without_root_hooks_leaves_target_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "root"
    child = tmp_path / "child"
    root.mkdir()
    child.mkdir()
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    atomic_config_write(root / "config.yaml", {"hooks": {"on_session_start": [{"command": "other"}]}})
    target = child / "config.yaml"
    atomic_config_write(target, {"model": {"default": "fixture-model"}})
    before = target.read_bytes()
    profiles._seed_beacon_hooks(child)
    assert target.read_bytes() == before
