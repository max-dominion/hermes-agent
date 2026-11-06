"""Exact sender/channel opt-in through real discord.py event objects."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
import plugins.platforms.discord.adapter as mod
from gateway.config import PlatformConfig

OWNER = 814634995401490464
BOT = 1546176276451303515
CHANNEL = 1559405067113734164
PEER = 1559570253619658772


def event(state, channel, author=OWNER, bot=False, mentioned=False):
    return discord.Message(state=state, channel=channel, data={
        'id': '1568290999999999999', 'channel_id': str(channel.id),
        'author': {'id': str(author), 'username': 'fixture', 'discriminator': '0',
                   'avatar': None, 'bot': bot},
        'content': (f'<@{BOT}> ' if mentioned else '') + 'health check',
        'type': 0, 'timestamp': datetime.now(timezone.utc).isoformat(),
        'edited_timestamp': None, 'tts': False, 'mention_everyone': False,
        'mentions': ([{'id': str(BOT), 'username': 'agent', 'discriminator': '0',
                       'avatar': None, 'bot': True}] if mentioned else []),
        'mention_roles': [], 'attachments': [], 'embeds': [], 'pinned': False,
        'flags': 0, 'components': [],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('case,expected', [
    ('owner_here', True), ('owner_elsewhere', False), ('other_human', False),
    ('peer_bot', False), ('owner_bot_spoof', False), ('child_thread', False),
    ('owner_recovered', False), ('owner_mentioned_elsewhere', True),
    ('trusted_bot_mentioned', True), ('unallowed_owner', False),
    ('feature_absent', False), ('wrong_type', False),
])
async def test_exact_live_exception(monkeypatch, case, expected):
    monkeypatch.setenv('DISCORD_AUTO_THREAD', 'false')
    monkeypatch.setenv('DISCORD_ALLOW_BOTS', 'mentions')
    state = discord.state.ConnectionState(dispatch=lambda *a: None, handlers={}, hooks={},
                                         http=NS(), intents=discord.Intents.default())
    guild = discord.Guild(state=state, data={'id': '1546161214479007834',
        'name': 'fixture', 'roles': [], 'channels': [], 'members': []})
    channel = discord.TextChannel(state=state, guild=guild, data={
        'id': str(CHANNEL + (case in {'owner_elsewhere', 'owner_mentioned_elsewhere'})),
        'type': 0, 'name': 'fixture', 'position': 0, 'permission_overwrites': []})
    if case == 'child_thread':
        channel = discord.Thread(state=state, guild=guild, data={
            'id': str(CHANNEL+2), 'parent_id': str(CHANNEL), 'owner_id': str(OWNER),
            'name': 'child', 'type': 11, 'message_count': 0, 'member_count': 0,
            'rate_limit_per_user': 0, 'thread_metadata': {'archived': False,
                'auto_archive_duration': 60, 'archive_timestamp': datetime.now(timezone.utc).isoformat(),
                'locked': False, 'invitable': True}})
    extra = {'strict_explicit_mentions': True, 'require_mention': True,
             'thread_require_mention': True, 'free_response_channels': '',
             'bots_require_inline_mention': True, 'history_backfill': False,
             'trusted_bot_ids': [str(PEER)],
             'mention_free_channel_users': {str(CHANNEL): [str(OWNER)]}}
    if case == 'feature_absent':
        extra.pop('mention_free_channel_users')
    if case == 'wrong_type':
        extra['mention_free_channel_users'] = {str(CHANNEL): str(OWNER)}
    a = mod.DiscordAdapter(PlatformConfig(enabled=True, token='fixture', extra=extra))
    a._client = NS(user=NS(id=BOT, bot=True), get_guild=lambda _: guild)
    a._allowed_user_ids = {str(OWNER), '42'} if case != 'unallowed_owner' else {'42'}
    a._allowed_role_ids = set()
    a._ready_event.set()
    a._text_batch_delay_seconds = 0
    a.handle_message = AsyncMock()
    message = event(state, channel,
                    author=PEER if case in {'peer_bot','trusted_bot_mentioned'} else 42 if case == 'other_human' else OWNER,
                    bot=case in {'peer_bot','trusted_bot_mentioned','owner_bot_spoof'},
                    mentioned=case in {'owner_mentioned_elsewhere','trusted_bot_mentioned'})
    if case == 'owner_recovered':
        assert a._discord_message_admission(message, claim=False) == (False, False)
        a._handle_message = AsyncMock()
        assert await a._dispatch_recovered_message(message) is False
        a._handle_message.assert_not_awaited()
    else:
        await a._dispatch_discord_message(message)
    assert bool(a.handle_message.await_count) is expected
