"""One provider's credential identity inside ``auth.json``, for the model-catalog disk cache key.

The catalog cache (``provider_models_cache.json``) is keyed by a credential fingerprint. It used to
hash the whole ``auth.json`` file signature (mtime, size, inode, ctime) plus its digest, so ANY write
to the file discarded every API-key provider's cached catalog: an OAuth token refresh, a pool request
counter, a cooldown status, another provider's login. The non-blocking ``/model`` picker then saw a
fingerprint mismatch and showed the curated static list until a background refresh landed, which hid
every live-only model (``claude-fable-5-2`` on the Discord picker). Only this provider's credential
identity belongs in its key: adding, removing or re-keying a credential still invalidates the row;
routine bookkeeping and token rotation do not (the same rule the Codex fingerprint already follows).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Pool-entry fields that name WHICH credential an entry is. Rotating tokens, counters, status and
# error fields are left out on purpose: they change on routine use, not on an account change.
_POOL_IDENTITY_FIELDS = ("id", "auth_type", "source", "base_url", "label", "secret_fingerprint")
# ``providers.<id>`` login-state fields that name the account or endpoint, never the tokens.
_STATE_IDENTITY_FIELDS = (
    "auth_type", "base_url", "portal_base_url", "inference_base_url", "account_id", "client_id", "scope")
# A static API key is its own identity (re-keying must invalidate); OAuth access tokens rotate.
_STATIC_KEY_AUTH_TYPES = frozenset({"api_key"})


def _digest(value: str) -> str:
    return hashlib.blake2b(value.encode("utf-8", errors="replace"), digest_size=8).hexdigest()


def _pool_identity(entries: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        row = {k: entry[k] for k in _POOL_IDENTITY_FIELDS if entry.get(k) is not None}
        token = entry.get("access_token")
        if entry.get("auth_type") in _STATIC_KEY_AUTH_TYPES and isinstance(token, str) and token:
            row["key"] = _digest(token)
        rows.append(row)
    return rows


def profile_env_value(name: str) -> str:
    """The value this profile configures for env var *name*, the same in every process on the home.

    The catalog cache is one file per home, shared by the gateway and every Hermes child on it. The
    terminal and cron sanitizers scrub provider keys from a child's ``os.environ``, so a key read only
    from ``os.environ`` split one profile's row into two cache keys: the child's write evicted the
    gateway's row and the next picker open showed the curated list (no ``claude-fable-5-2``). Read the
    bound profile scope, then ``os.environ``, then the home's own ``.env``."""
    from agent.secret_scope import UnscopedSecretError, get_secret, load_env_file
    try:
        value = get_secret(name)
    except UnscopedSecretError:  # an unbound multiplex read: this home's .env, never another profile's
        value = None
    return value or load_env_file(home_auth_file().with_name(".env")).get(name, "")


def home_auth_file() -> Path:
    """The active profile's ``auth.json`` (resolved per call: one process serves several homes)."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "auth.json"


def auth_store_identity(auth_file: Path, provider: str) -> str:
    """Stable text naming *provider*'s credentials in *auth_file* (``"missing"`` when absent).

    An unreadable or malformed file yields a marker rather than raising: the caller hashes the
    result, so the worst case is one cache miss."""
    try:
        raw = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError) as exc:
        return f"unreadable:{type(exc).__name__}"
    if not isinstance(raw, dict):
        return "malformed"

    def _section(name: str) -> Any:
        section = raw.get(name)
        return section.get(provider) if isinstance(section, dict) else None

    state = _section("providers")
    identity = {
        "pool": _pool_identity(_section("credential_pool")),
        "state": ({k: state[k] for k in _STATE_IDENTITY_FIELDS if state.get(k) is not None}
                  if isinstance(state, dict) else None),
        "suppressed": _section("suppressed_sources"),
    }
    return _digest(json.dumps(identity, sort_keys=True, default=str))
