"""``oauth.extra_authorize_params`` reaches the authorization URL the user opens.

Dropbox issues a refresh token only when the authorize URL carries
``token_access_type=offline``; without it every grant dies after four hours.
"""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp.client.auth")

from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402

import tools.mcp_oauth as mo  # noqa: E402


class _Opened(Exception):
    """Stops the flow once the URL is handed to the browser step."""


def _build(builder: str, name: str, oauth_cfg: dict):
    if builder == "legacy":
        return mo.build_oauth_auth(name, "https://mcp.example.com/mcp", oauth_cfg)
    from tools.mcp_oauth_manager import MCPOAuthManager
    return MCPOAuthManager().get_or_build_provider(name, "https://mcp.example.com/mcp", oauth_cfg)


def _authorize_query(builder: str, name: str, oauth_cfg: dict, monkeypatch) -> dict[str, list[str]]:
    """Run the real SDK authorization-code grant until the URL would open; return its query."""
    seen: list[str] = []

    def _announce(url, port, redirect_uri):
        seen.append(url)
        raise _Opened

    monkeypatch.setattr(mo, "_announce_authorization_url", _announce)
    monkeypatch.setattr(mo, "_is_interactive", lambda: True)
    provider = _build(builder, name, oauth_cfg)
    assert provider is not None
    redirect = str(provider.context.client_metadata.redirect_uris[0])
    provider.context.client_info = OAuthClientInformationFull.model_validate(
        {"client_id": "cid", "redirect_uris": [redirect], "token_endpoint_auth_method": "none"})
    with pytest.raises(_Opened):
        asyncio.run(provider._perform_authorization_code_grant())
    assert len(seen) == 1
    return parse_qs(urlsplit(seen[0]).query)


@pytest.mark.parametrize("builder", ["legacy", "manager"])
def test_configured_params_reach_authorize_url_without_overriding_sdk_params(builder, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    query = _authorize_query(builder, "dropbox", {"extra_authorize_params": {
        "token_access_type": "offline", "state": "attacker-chosen"}}, monkeypatch)

    assert query["token_access_type"] == ["offline"]
    # PKCE/state belong to the SDK; a config value must never replace them.
    assert query["state"] != ["attacker-chosen"] and len(query["state"]) == 1
    assert query["client_id"] == ["cid"]


@pytest.mark.parametrize("builder", ["legacy", "manager"])
def test_no_extra_params_key_leaves_authorize_url_unchanged(builder, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with_key = _authorize_query(builder, "with-key", {"extra_authorize_params": {"token_access_type": "offline"}},
                                monkeypatch)
    without_key = _authorize_query(builder, "without-key", {}, monkeypatch)

    assert "token_access_type" not in without_key
    assert set(without_key) == set(with_key) - {"token_access_type"}
