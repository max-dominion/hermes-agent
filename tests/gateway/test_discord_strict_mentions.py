"""Fleet opt-in policy through native ingress; SDK events are safe fixtures, not live Discord."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import plugins.platforms.discord.adapter as mod
from gateway.config import PlatformConfig

BOT_IDS = [1546176276451303515, 1558134017428029652, 1558134874710474855,
           1559570253619658772, 1559571151741788330, 1564324127307858062,
           1564324178692407527, 1564324245075402812]

class Channel:
    id = 1551238575927070770
    name = 'fixture'
    parent_id = 123
    parent = None
    topic = None
    guild = NS(id=1, name='fixture')
    def history(self, **kw):
        async def it():
            if False:
                yield
        return it()

class DM(Channel):
    guild = None

@pytest.mark.asyncio
@pytest.mark.parametrize('own_id', BOT_IDS)
@pytest.mark.parametrize('case,expected', [
    ('unmentioned', False), ('bare_name', False), ('wrong_agent', False),
    ('everyone', False), ('role', False), ('reply_ping', False),
    ('code_no_sdk', False), ('forward_no_sdk', False), ('sdk_only', False),
    ('own', True), ('legacy_own', True), ('multi', True),
    ('trusted_bot', True), ('unknown_bot', False), ('stranger', False),
    ('dm', True), ('joined_thread', False), ('free_parent', False),
    ('voice_channel', False),
])
async def test_strict_native_dispatch(monkeypatch, own_id, case, expected, caplog):
    for k in list(mod.os.environ):
        if k.startswith('DISCORD_'):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('DISCORD_ALLOW_BOTS', 'mentions')
    monkeypatch.setenv('DISCORD_AUTO_THREAD', 'false')
    monkeypatch.setattr(mod.discord, 'Thread', Channel)
    monkeypatch.setattr(mod.discord, 'DMChannel', DM)
    peer = next(x for x in BOT_IDS if x != own_id)
    config = PlatformConfig(enabled=True, token='fixture', extra={
        'require_mention': True, 'thread_require_mention': True,
        'free_response_channels': str(Channel.parent_id),
        'bots_require_inline_mention': True, 'history_backfill': False,
        'strict_explicit_mentions': True, 'trusted_bot_ids': [str(x) for x in BOT_IDS],
    })
    a = mod.DiscordAdapter(config)
    own = NS(id=own_id, bot=True)
    a._client = NS(user=own, get_guild=lambda _: None)
    a._allowed_user_ids = {'42'}
    a._allowed_role_ids = set()
    a._threads.mark(str(Channel.id))
    a._ready_event.set()
    a._text_batch_delay_seconds = 0
    a.handle_message = AsyncMock()
    channel = DM() if case == 'dm' else Channel()
    content, mentions = 'hello', []
    if case in {'own','multi','trusted_bot','unknown_bot','stranger'}:
        content, mentions = f'<@{own_id}> hello', [own]
    if case == 'legacy_own':
        content, mentions = f'<@!{own_id}> hello', [own]
    if case == 'multi':
        content += f' <@{peer}>'
        mentions.append(NS(id=peer, bot=True))
    if case in {'reply_ping','sdk_only'}:
        mentions = [own]
    if case == 'code_no_sdk':
        content = f'`<@{own_id}>`'
    if case == 'forward_no_sdk':
        content = f'> <@{own_id}>'
    if case == 'bare_name':
        content = '@Max hello'
    if case == 'wrong_agent':
        content, mentions = f'<@{peer}> hello', [NS(id=peer, bot=True)]
    if case == 'everyone':
        content = '@everyone hello'
    if case == 'role':
        content = '<@&999> hello'
    if case == 'voice_channel':
        a._voice_text_channels = {1: channel.id}
    author = NS(id=peer if case == 'trusted_bot' else 666 if case in {'unknown_bot','stranger'} else 42,
                bot=case in {'trusted_bot','unknown_bot'}, name='fixture', display_name='fixture', roles=[])
    msg = NS(id=123,content=content,mentions=mentions,attachments=[],
             reference=NS(message_id=1,resolved=NS(id=1,content='old',author=own,attachments=[])) if case == 'reply_ping' else None,
             created_at=datetime.now(timezone.utc),channel=channel,guild=channel.guild,author=author,
             type=mod.discord.MessageType.reply if case == 'reply_ping' else mod.discord.MessageType.default,
             message_snapshots=[])
    with caplog.at_level('INFO'):
        await a._dispatch_discord_message(msg)
    assert bool(a.handle_message.await_count) is expected
    if not expected and case != 'stranger':
        assert 'no_model_work=true' in caplog.text

@pytest.mark.asyncio
async def test_recovery_uses_strict_admission(monkeypatch):
    monkeypatch.setattr(mod.discord, 'Thread', Channel)
    a = mod.DiscordAdapter(PlatformConfig(enabled=True,token='fixture',extra={
        'strict_explicit_mentions':True, 'require_mention':False,
        'free_response_channels':'*', 'thread_require_mention':False}))
    own = NS(id=999,bot=True)
    a._client=NS(user=own)
    a._handle_message=AsyncMock()
    m=NS(id=123,content='hello',mentions=[own],channel=Channel(),author=NS(id=42,bot=False),type=mod.discord.MessageType.reply)
    assert await a._dispatch_recovered_message(m) is False
    a._handle_message.assert_not_awaited()

def test_policy_is_opt_in():
    a=mod.DiscordAdapter(PlatformConfig(enabled=True,token='fixture'))
    assert a.config.extra.get('strict_explicit_mentions') is not True
