// Pure decision helpers for in-process Spectrum client recovery.
//
// spectrum-ts 12.x builds its iMessage client set once, from the token payload
// minted at startup. Two upstream gaps leave that set stale for the life of the
// process when the project's plan changes underneath it:
//
//   shared -> dedicated (Free/Pro -> Business): the scheduled token refresh
//     reconciles the new dedicated line IN, but never removes the original
//     `"shared"` client, whose token getter now returns `undefined` (the
//     dedicated payload has no `token`). Sends pick a client at random and fail
//     with `AuthenticationError: Invalid token`; `space.get` fails with
//     "requires params.phone when multiple clients are configured". The shared
//     client was built without a line observer, so the new line never gets an
//     inbound subscription either: replies to it are silently dropped.
//   dedicated -> shared: the refresh returns early for a shared payload and the
//     dedicated clients keep resolving `tokenData.auth[...]` on an object that
//     no longer has `auth`, so every call fails until restart.
//
// Hermes cannot reach into the SDK's client array, so the recovery is to build
// a fresh Spectrum app (fresh token, current line set) and swap it in. These
// helpers decide WHEN; index.mjs does the swap. Pure, so tests run them under
// node — see tests/plugins/platforms/photon/test_auth_recovery.py.

const AUTH_TEXT_RE = /AuthenticationError|invalid token|unauthenticated|permission[_ ]denied|token (?:is )?expired/i;
const MULTI_CLIENT_RE = /multiple clients are configured\. Available: ([^\n]+)/;
// gRPC UNAUTHENTICATED (16) / PERMISSION_DENIED (7), as surfaced by
// @photon-ai/advanced-imessage's fromGrpcError.
const AUTH_GRPC_CODES = new Set([7, 16]);

function* causeChain(err) {
  let cur = err;
  for (let depth = 0; cur && depth < 6; depth++) {
    yield cur;
    cur = typeof cur === "object" ? cur.cause : undefined;
  }
}

function messageOf(err) {
  if (err && typeof err === "object" && err.message) return String(err.message);
  return String(err);
}

/**
 * Parse the client list out of spectrum's multi-client `space.get` error.
 * @returns {string[] | null} e.g. ["shared", "+16285550100"], or null
 */
export function parseAvailableClients(err) {
  for (const e of causeChain(err)) {
    const m = messageOf(e).match(MULTI_CLIENT_RE);
    if (m) {
      return m[1]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }
  }
  return null;
}

/**
 * A client set that mixes the shared-pool sentinel with dedicated numbers can
 * only come from a plan change the SDK failed to reconcile.
 */
export function isMixedClientSet(clients) {
  if (!Array.isArray(clients) || clients.length < 2) return false;
  return clients.includes("shared") && clients.some((c) => c !== "shared");
}

/**
 * True when an error (or anything in its cause chain) means the live client's
 * credentials or line set are stale — the class a client rebuild can fix.
 * Network/transient failures are NOT auth failures: rebuilding would not help.
 */
export function isAuthFailure(err) {
  if (err == null) return false;
  for (const e of causeChain(err)) {
    if (e && typeof e === "object") {
      if (e.name === "AuthenticationError") return true;
      if (AUTH_GRPC_CODES.has(e.grpcCode)) return true;
    }
    const msg = messageOf(e);
    if (AUTH_TEXT_RE.test(msg)) return true;
    if (isMixedClientSet(parseAvailableClients(e))) return true;
  }
  return false;
}

/**
 * Whether the project's iMessage mode has changed since the running client
 * set was built. Unknown values never count as drift.
 */
export function isModeDrift(builtMode, currentMode) {
  const known = (m) => m === "shared" || m === "dedicated";
  return known(builtMode) && known(currentMode) && builtMode !== currentMode;
}

/**
 * Rate limit for rebuilds: a persistent upstream fault must not turn into a
 * rebuild loop that hammers Photon's token endpoint.
 */
export function rebuildAllowed(now, lastRebuildAt, minIntervalMs) {
  if (!(lastRebuildAt > 0)) return true;
  return now - lastRebuildAt >= minIntervalMs;
}
