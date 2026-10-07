"""Photon sidecar self-healing on stale auth (628 main-line incident, 2026-10-07).

spectrum-ts 12.x builds its iMessage client set once. When the project moved
from the shared pool to a dedicated (Business) line under a running sidecar,
the SDK's token refresh added the new line but kept the old ``"shared"``
client, whose token is now ``undefined``. Sends hit ``AuthenticationError:
Invalid token`` / "multiple clients are configured" and the new line never got
an inbound stream, until a manual gateway restart.

The sidecar now rebuilds its Spectrum client in place (fresh token, current
line set) and retries the failed operation once. These tests run the REAL
``index.mjs`` against a fake ``spectrum-ts`` whose first client is stale and
whose second is healthy — no network, no real Photon traffic.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

_SIDECAR = Path("plugins/platforms/photon/sidecar")

_FAKE_SDK = textwrap.dedent(
    """
    // Each Spectrum() call is one client generation. Generation 1 is the
    // stale client from the incident; generation 2+ is healthy.
    let generation = 0;
    const never = () => new Promise(() => {});
    function staleAuthError() {
      const e = new Error("[spectrum-imessage] Invalid token");
      e.name = "AuthenticationError";
      e.grpcCode = 16;
      return e;
    }
    function inboundOnce(gen) {
      let sent = false;
      return {
        [Symbol.asyncIterator]() {
          return {
            next() {
              if (gen >= 2 && !sent) {
                sent = true;
                const space = { id: "any;-;+15555550123", type: "dm", phone: "+15555550123",
                                send: async () => ({ id: `sent-by-generation-${gen}` }) };
                return Promise.resolve({ done: false, value: [space, {
                  id: "inbound-after-rebuild", direction: "inbound", space,
                  sender: { id: "+15555550123" },
                  content: { type: "text", text: "reply reached the new line" },
                  timestamp: new Date(),
                }] });
              }
              return never();
            },
          };
        },
      };
    }
    export async function Spectrum() {
      const gen = ++generation;
      return { __gen: gen, messages: inboundOnce(gen), stop: async () => undefined };
    }
    export const attachment = (v) => v;
    export const voice = (v) => v;
    export const text = (v) => ({ type: "text", text: v });
    export const markdown = (v) => ({ type: "markdown", text: v });
    export const richlink = (v) => v;
    export const typing = (v) => v;
    export const poll = (v) => v;
    """
).lstrip()

_FAKE_IMESSAGE = textwrap.dedent(
    """
    export function imessage(app) {
      const stale = app.__gen === 1;
      const space = (id) => ({
        id,
        send: async () => ({ id: `sent-by-generation-${app.__gen}` }),
        getMessage: async () => { const e = new Error("not found"); e.code = 5; throw e; },
      });
      return {
        space: {
          create: async (phone) => {
            if (stale) {
              const e = new Error("[spectrum-imessage] Invalid token");
              e.name = "AuthenticationError";
              e.grpcCode = 16;
              throw e;
            }
            return space(`any;-;${phone}`);
          },
          get: async (id) => {
            if (stale) throw new Error(
              "iMessage space.get requires params.phone when multiple clients are configured. " +
              "Available: shared, +16285550100");
            return space(id);
          },
        },
      };
    }
    imessage.config = () => ({});
    """
).lstrip()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_fixture(tmp_path: Path) -> Path:
    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    for src in _SIDECAR.glob("*.mjs"):
        shutil.copyfile(src, sidecar / src.name)
    pkg = sidecar / "node_modules" / "spectrum-ts"
    (pkg / "providers").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({
        "name": "spectrum-ts", "type": "module",
        "exports": {".": "./index.js", "./providers/imessage": "./providers/imessage.js"},
    }), encoding="utf-8")
    (pkg / "index.js").write_text(_FAKE_SDK, encoding="utf-8")
    (pkg / "providers" / "imessage.js").write_text(_FAKE_IMESSAGE, encoding="utf-8")
    return sidecar


def _call(port: int, path: str, body: Dict[str, Any] | None = None) -> tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body or {}).encode(),
        headers={"X-Hermes-Sidecar-Token": "test-token"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _start(sidecar: Path, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "PHOTON_PROJECT_ID": "test-project", "PHOTON_PROJECT_SECRET": "test-secret",
        "PHOTON_SIDECAR_PORT": str(port), "PHOTON_SIDECAR_TOKEN": "test-token",
        # Never reach real Photon cloud from a test: mode lookups fail fast.
        "SPECTRUM_CLOUD_URL": "http://127.0.0.1:9",
        "PHOTON_AUTH_CANARY_MS": "0",
    }
    proc = subprocess.Popen(["node", "index.mjs"], cwd=sidecar, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 10
    while True:
        try:
            status, _ = _call(port, "/healthz")
            if status == 200:
                return proc
        except OSError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("sidecar did not start:\n" + (proc.stdout.read() if proc.stdout else ""))
        time.sleep(0.05)


def _read_inbound_line(port: int, timeout: float = 10.0) -> Dict[str, Any]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/inbound",
                                 headers={"X-Hermes-Sidecar-Token": "test-token"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            line = resp.readline().decode().strip()
            if line:
                return json.loads(line)


def test_stale_auth_send_rebuilds_client_and_retries_once(tmp_path: Path) -> None:
    """The incident path: a send through a stale client must succeed after an
    in-process rebuild, without a gateway restart — and the rebuilt client's
    inbound stream must reach the consumer (the reply that was silently lost)."""
    sidecar = _write_fixture(tmp_path)
    port = _free_port()
    proc = _start(sidecar, port)
    try:
        status, payload = _call(port, "/send", {"spaceId": "+15555550123", "text": "hi"})
        assert status == 200, payload
        assert payload["messageId"] == "sent-by-generation-2"

        status, health = _call(port, "/healthz")
        assert status == 200
        assert health["auth"]["rebuilds"] == 1
        assert health["auth"]["generation"] == 2

        event = _read_inbound_line(port)
        assert event["messageId"] == "inbound-after-rebuild"
        assert event["content"]["text"] == "reply reached the new line"
    finally:
        proc.terminate()
        proc.communicate(timeout=5)


def test_auth_failure_classification_separates_auth_from_transient() -> None:
    """Rebuild only for stale credentials / an unreconciled client set — never
    for network faults or the probe's expected not-found, which a rebuild can't fix."""
    module = (_SIDECAR / "auth-recovery.mjs").resolve().as_uri()
    script = textwrap.dedent(
        f"""
        import {{ isAuthFailure, isModeDrift }} from {json.dumps(module)};
        const authErr = Object.assign(new Error("[spectrum-imessage] Invalid token"),
                                      {{ name: "AuthenticationError", grpcCode: 16 }});
        const wrapped = new Error("unable to resolve space id +1555", {{ cause: authErr }});
        const mixed = new Error("iMessage space.get requires params.phone when multiple " +
                                "clients are configured. Available: shared, +16285550100");
        const twoDedicated = new Error("iMessage space.get requires params.phone when multiple " +
                                       "clients are configured. Available: +16285550100, +14155550100");
        const unavailable = Object.assign(new Error("14 UNAVAILABLE: connect failed"), {{ grpcCode: 14 }});
        const notFound = Object.assign(new Error("message not found"), {{ code: 5 }});
        console.log(JSON.stringify({{
          auth: isAuthFailure(authErr), wrapped: isAuthFailure(wrapped), mixed: isAuthFailure(mixed),
          twoDedicated: isAuthFailure(twoDedicated), unavailable: isAuthFailure(unavailable),
          notFound: isAuthFailure(notFound),
          drift: isModeDrift("shared", "dedicated"), same: isModeDrift("dedicated", "dedicated"),
          unknown: isModeDrift(null, "dedicated"),
        }}));
        """
    )
    run = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out == {
        "auth": True, "wrapped": True, "mixed": True, "twoDedicated": False,
        "unavailable": False, "notFound": False,
        "drift": True, "same": False, "unknown": False,
    }


def test_heartbeat_patch_routes_server_heartbeats_to_sidecar_hook(tmp_path: Path) -> None:
    """spectrum-ts drops Photon's ~30 s heartbeat frames (no onHeartbeat); the
    patch must route them to the sidecar's hook, so quiet lines stay 'alive'."""
    dist = tmp_path / "node_modules" / "@spectrum-ts" / "imessage" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.js").write_text(textwrap.dedent(
        """
        export function build(createGrpcClient) {
        \treturn createGrpcClient({
        \t\taddress: "x:443",
        \t\tautoIdempotency: true,
        \t\tretry: true
        \t});
        }
        """
    ).lstrip(), encoding="utf-8")
    patcher = (_SIDECAR / "patch-spectrum-heartbeat.mjs").resolve()
    for _ in range(2):  # idempotent
        run = subprocess.run(["node", str(patcher), str(tmp_path)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    script = textwrap.dedent(
        f"""
        import {{ HEARTBEAT_HOOK }} from {json.dumps(patcher.as_uri())};
        import {{ build }} from {json.dumps((dist / "index.js").as_uri())};
        let beats = 0;
        globalThis[HEARTBEAT_HOOK] = () => {{ beats += 1; }};
        const opts = build((o) => o);
        opts.onHeartbeat(); opts.onHeartbeat();
        delete globalThis[HEARTBEAT_HOOK];
        opts.onHeartbeat();  // hook absent: must be a harmless no-op
        console.log(JSON.stringify({{ beats, retry: opts.retry }}));
        """
    )
    run = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"beats": 2, "retry": True}
