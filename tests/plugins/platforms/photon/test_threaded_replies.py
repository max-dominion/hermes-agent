"""iMessage threaded replies ("Reply" on a specific bubble) over Photon.

The sidecar forwards ``{type: "reply", content: <inner>, targetMessageId,
targetDirection, targetText}``. The user's words live in the inner content;
the quoted bubble must reach the gateway's ``reply_to_*`` fields rather than
being dropped as an unhandled content type.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.photon.adapter import PhotonAdapter

_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhf"
    "DwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    return PhotonAdapter(PlatformConfig(enabled=True, token="", extra={}))


def _capture(adapter: PhotonAdapter, monkeypatch: pytest.MonkeyPatch) -> List[MessageEvent]:
    captured: List[MessageEvent] = []

    async def fake_handle(event: MessageEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    return captured


def _reply_event(inner: Dict[str, Any], **target: Any) -> Dict[str, Any]:
    return {
        "messageId": "spc-reply-1",
        "platform": "iMessage",
        "space": {"id": "+155****4567", "type": "dm", "phone": "+155****4567"},
        "sender": {"id": "+155****4567"},
        "content": {"type": "reply", "content": inner, **target},
        "timestamp": "2026-10-07T19:40:00.000Z",
    }


@pytest.mark.asyncio
async def test_threaded_text_reply_keeps_words_and_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_inbound(_reply_event(
        {"type": "text", "text": "Yes, book the 3pm."},
        targetMessageId="bot-msg-9", targetDirection="outbound",
        targetText="Want the 3pm or the 4pm slot?"))

    assert len(captured) == 1
    ev = captured[0]
    assert ev.text == "Yes, book the 3pm."
    assert "not handled" not in ev.text
    assert ev.reply_to_message_id == "bot-msg-9"
    assert ev.reply_to_text == "Want the 3pm or the 4pm slot?"
    assert ev.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_threaded_reply_wrapping_attachment_caches_media(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)
    raw = base64.b64decode(_PNG_1X1_B64)

    await adapter._dispatch_inbound(_reply_event(
        {"type": "attachment", "name": "photo.png", "mimeType": "image/png",
         "size": len(raw), "data": _PNG_1X1_B64, "encoding": "base64"},
        targetMessageId="their-msg-1", targetDirection="inbound", targetText="send me the pic"))

    assert len(captured) == 1
    ev = captured[0]
    assert ev.message_type == MessageType.PHOTO
    assert len(ev.media_urls) == 1
    cached = Path(ev.media_urls[0])
    try:
        assert cached.read_bytes() == raw
    finally:
        cached.unlink(missing_ok=True)
    assert ev.reply_to_message_id == "their-msg-1"
    assert ev.reply_to_text == "send me the pic"
    assert ev.reply_to_is_own_message is False


@pytest.mark.asyncio
async def test_threaded_reply_unknown_inner_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch)
    captured = _capture(adapter, monkeypatch)

    await adapter._dispatch_inbound(_reply_event(
        {"type": "sticker"}, targetMessageId="bot-msg-2", targetText=None))

    assert len(captured) == 1
    ev = captured[0]
    assert ev.text == "[Photon content type not handled: sticker]"
    assert ev.reply_to_message_id == "bot-msg-2"
    assert ev.reply_to_text is None
