#!/usr/bin/env node
// Patch spectrum-ts' iMessage provider so server heartbeat frames reach the
// sidecar's zombie-stream watchdog.
//
// Photon's iMessage server sends a heartbeat frame on every subscribe stream
// about every 30 s, even when there is no traffic. @photon-ai/advanced-imessage
// surfaces those frames through the `onHeartbeat` option of
// `createGrpcClient`, and documents that without it "heartbeat frames are
// silently dropped and a half-open stall is undetectable". spectrum-ts (12.x)
// never passes `onHeartbeat`, so the sidecar can only measure liveness by
// message traffic — and a healthy but quiet line looks exactly like a dead
// one. Hermes' watchdog then restarted a healthy stream every 10 minutes.
//
// The patch adds `onHeartbeat` to each `createGrpcClient({...})` call in
// `@spectrum-ts/imessage/dist`, routed through a process-global hook the
// sidecar installs. If the hook is absent the call is a no-op, so the patched
// SDK still works standalone. Idempotent; fails loudly when a future
// spectrum-ts reshapes the anchor (README "Upgrading spectrum-ts").
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const HEARTBEAT_HOOK = "__hermesPhotonHeartbeat";
const MARKER = "Hermes patch: route iMessage server heartbeats to the sidecar";
const ANCHOR = "autoIdempotency: true,";
const INJECT =
  `onHeartbeat: () => globalThis.${HEARTBEAT_HOOK}?.(),\n`;

function scriptDir() {
  return path.dirname(fileURLToPath(import.meta.url));
}

function patchSource(source) {
  // Each createGrpcClient({ ... }) in the provider opens with this exact line;
  // inject onHeartbeat beside it, matching the line's indentation.
  const lines = source.split("\n");
  let count = 0;
  const out = [];
  for (const line of lines) {
    out.push(line);
    if (line.trim() === ANCHOR) {
      const indent = line.slice(0, line.indexOf(ANCHOR));
      out.push(indent + INJECT.trimEnd());
      count += 1;
    }
  }
  if (count === 0) {
    throw new Error(`no createGrpcClient anchor (${ANCHOR}) found`);
  }
  return { patched: out.join("\n"), count };
}

export function patchSpectrumHeartbeat(root = scriptDir()) {
  const dist = path.join(root, "node_modules", "@spectrum-ts", "imessage", "dist");
  if (!fs.existsSync(dist)) {
    throw new Error(`@spectrum-ts/imessage dist not found: ${dist}`);
  }
  for (const name of fs.readdirSync(dist)) {
    if (!name.endsWith(".js")) continue;
    const file = path.join(dist, name);
    const raw = fs.readFileSync(file, "utf8");
    if (raw.includes(MARKER)) return { patched: false, file, reason: "already patched" };
    if (!raw.includes("createGrpcClient") || !raw.includes(ANCHOR)) continue;
    const CRLF = String.fromCharCode(13) + "\n";
    const usedCRLF = raw.includes(CRLF);
    const original = usedCRLF ? raw.split(CRLF).join("\n") : raw;
    const { patched, count } = patchSource(original);
    let text = `// ${MARKER}\n${patched}`;
    if (usedCRLF) text = text.split("\n").join(CRLF);
    fs.writeFileSync(file, text, "utf8");
    return { patched: true, file, count };
  }
  throw new Error("could not find @spectrum-ts/imessage createGrpcClient chunk to patch");
}

const _invokedDirectly =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (_invokedDirectly) {
  try {
    const root = process.argv[2] ? path.resolve(process.argv[2]) : scriptDir();
    const result = patchSpectrumHeartbeat(root);
    console.error(
      `photon-sidecar: spectrum heartbeat patch ${result.patched ? "patched" : "ok"}: ${result.file}`
    );
  } catch (err) {
    console.error(`photon-sidecar: spectrum heartbeat patch failed: ${err?.stack || err}`);
    process.exit(1);
  }
}
