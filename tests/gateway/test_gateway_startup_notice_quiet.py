"""Startup-only quiet notices must not mute active-session warnings."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import gateway.run_notifications as notices
import gateway.run_shutdown as shutdown
from gateway.config import Platform, PlatformConfig

NOTICE_CLASS = next(v for v in vars(notices).values() if isinstance(v, type)
                    and '_send_home_channel_startup_notifications' in v.__dict__)
SHUTDOWN_CLASS = next(v for v in vars(shutdown).values() if isinstance(v, type)
                      and '_restart_notification_allowed' in v.__dict__)

@pytest.mark.asyncio
@pytest.mark.parametrize('quiet', [False, True])
async def test_startup_only_quiet_option(quiet):
    cfg = PlatformConfig(enabled=True, extra={'gateway_startup_notification': not quiet})
    home = NS(chat_id='fixture-home', thread_id=None)
    owner = NS(_home_channel_transports=lambda: [(Platform.DISCORD, cfg, home, NS())],
               _send_home_channel_message=AsyncMock(return_value=True))
    result = await NOTICE_CLASS._send_home_channel_startup_notifications(owner)
    assert owner._send_home_channel_message.await_count == (0 if quiet else 1)
    assert len(result) == (0 if quiet else 1)

def test_startup_quiet_does_not_mute_active_session_warnings():
    cfg = PlatformConfig(enabled=True, extra={'gateway_startup_notification': False})
    owner = NS(config=NS(platforms={Platform.DISCORD: cfg}))
    owner._restart_notification_allowed = lambda platform: SHUTDOWN_CLASS._restart_notification_allowed(owner, platform)
    assert owner._restart_notification_allowed(Platform.DISCORD) is True
    assert SHUTDOWN_CLASS._notice_allowed(owner, Platform.DISCORD, 'active session') is True

def test_config_preserves_startup_only_false_without_changing_restart_policy():
    cfg = PlatformConfig.from_dict({'enabled': True, 'extra': {'gateway_startup_notification': False}})
    assert cfg.extra['gateway_startup_notification'] is False
    assert cfg.gateway_restart_notification is True
