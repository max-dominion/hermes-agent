"""Exercise the real agent loop over a local HTTP transport (no paid model)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from hermes_cli import goals, kanban_db as kb, kanban_db_connect as kbc


@pytest.mark.parametrize("multipart", [False, True])
def test_real_agent_checkpoint_continues_with_history(tmp_path, monkeypatch, multipart):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(key, raising=False)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "messages" in body:
                requests.append(body)
            response = {"id": "completion", "object": "chat.completion", "created": 1,
                        "model": "checkpoint-test", "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": "checkpoint evidence"},
                        "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
            if body.get("stream"):
                response["object"] = "chat.completion.chunk"
                response["choices"][0]["delta"] = response["choices"][0].pop("message")
                payload = ("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                payload = json.dumps(response).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from run_agent import AIAgent
        import cli
        kb.init_db()
        with kbc.connect_closing() as conn:
            tid = kb.create_task(conn, title="Verify goal checkpoint", assignee="default", goal_mode=True, goal_max_turns=3)
            claimed = kb.claim_task(conn, tid, claimer="test:1")
        monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        agent = AIAgent(
            model="checkpoint-test", provider="openai-compat", api_key="test-key",
            base_url=f"http://127.0.0.1:{server.server_port}/v1", api_mode="chat_completions",
            max_iterations=5, enabled_toolsets=[], fallback_model=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
        original = agent.run_conversation
        turns = []

        def run(**kwargs):
            result = original(**kwargs)
            turns.append(result)
            assert not result.get("failed"), result
            if len(turns) == 2:
                with kbc.connect_closing() as conn:
                    assert kb.complete_task(conn, tid, summary="verified", expected_run_id=claimed.current_run_id)
            return result

        monkeypatch.setattr(agent, "run_conversation", run)
        monkeypatch.setattr(goals, "judge_goal", lambda *a, **k: ("continue", "next checkpoint", False, None, False))
        owner = SimpleNamespace(agent=agent, session_id=agent.session_id, conversation_history=[])
        with pytest.raises(SystemExit) as exc:
            query = [{"type": "text", "text": "produce checkpoint"}] if multipart else "produce checkpoint"
            cli._run_quiet_single_query(owner, query)
        assert exc.value.code == 0
        result = turns[-1]
        assert len(turns) == 2
        assert len(requests) == 2
        assert result["final_response"] == "checkpoint evidence"
        second = requests[1]["messages"]
        assert any(m.get("content") == "checkpoint evidence" for m in second)
        assert requests[0]["messages"][0] == second[0]  # byte-stable system prefix
        assert [m["role"] for m in second if m["role"] != "system"] == ["user", "assistant", "user"]
        assert owner.session_id == agent.session_id
        assert not agent._kanban_goal_loop_active
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
