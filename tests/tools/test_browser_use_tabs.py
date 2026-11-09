"""Tabs browser_exec opens on a shared CDP browser are closed at task cleanup (2026-11-09 OOM)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools import browser_use_shot, browser_use_tabs


class _FakeChrome:
    """Minimal DevTools HTTP endpoint: /json/list and /json/close/<id>."""

    def __init__(self, pages: dict[str, str]):
        self.pages = dict(pages)  # id -> url
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/json/list":
                    body = json.dumps([{"id": i, "type": "page", "url": u} for i, u in fake.pages.items()])
                elif self.path.startswith("/json/close/"):
                    tid = self.path.rsplit("/", 1)[-1]
                    if fake.pages.pop(tid, None) is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = "Target is closing"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.ws = f"ws://127.0.0.1:{self.server.server_address[1]}/devtools/browser/x"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def chrome():
    c = _FakeChrome({
        "AAAAAAAA0001": "https://app.notion.com/p/report",      # the task's QA tab
        "AAAAAAAA0002": "https://accounts.example.com/signin?x=1",  # the task's sign-in tab
        "BBBBBBBB0001": "https://app.notion.com/p/other",        # another task's tab
    })
    yield c
    c.close()


def test_task_cleanup_closes_only_its_own_tabs(tmp_path, chrome):
    env = {"BH_RUNTIME_DIR": str(tmp_path), "BU_NAME": "qa"}
    daemon = ("x", "qa")
    browser_use_tabs.register_call(env, daemon, "task-a", chrome.ws)
    ledger = env[browser_use_tabs.LEDGER_ENV]
    with open(ledger, "a") as f:
        f.write("AAAAAAAA0001\nAAAAAAAA0002\nAAAAAAAA0009\n")  # 0009: closed by the agent already

    assert browser_use_tabs.daemons_of_task("task-a") == [daemon]
    browser_use_tabs.release_daemon_tabs(daemon, env, "task-a")

    assert set(chrome.pages) == {"AAAAAAAA0002", "BBBBBBBB0001"}  # sign-in kept, other task untouched
    assert browser_use_tabs.daemons_of_task("task-a") == []


def test_shared_daemon_is_stopped_only_when_no_other_task_drives_it(tmp_path, chrome):
    env = {"BH_RUNTIME_DIR": str(tmp_path), "BU_NAME": "default"}
    daemon = ("x", "default")
    browser_use_tabs.register_call(dict(env), daemon, "task-a", chrome.ws)
    browser_use_tabs.register_call(dict(env), daemon, "task-b", chrome.ws)
    assert not browser_use_tabs.is_daemon_idle_after(daemon, "task-a")
    browser_use_tabs.release_daemon_tabs(daemon, env, "task-a")
    assert browser_use_tabs.is_daemon_idle_after(daemon, "task-b")
    browser_use_tabs.forget([daemon])


def test_harness_new_tab_is_recorded_in_the_ledger(tmp_path):
    ledger = tmp_path / "tabs"
    made = iter(["CCCCCCCC0001", "CCCCCCCC0002"])
    new_tab = browser_use_shot.make_recording_new_tab(lambda url="about:blank": next(made), str(ledger))
    assert new_tab("https://example.com") == "CCCCCCCC0001"
    new_tab()
    assert ledger.read_text().split() == ["CCCCCCCC0001", "CCCCCCCC0002"]
