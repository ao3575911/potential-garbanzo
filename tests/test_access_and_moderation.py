from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions
from telegram.error import NetworkError

from circuit_bot.access import Access
from circuit_bot.moderation import Moderation, duration
from circuit_bot.service import UserError


async def test_owner_cannot_bypass_missing_bot_permissions(config, fake_bot):
    fake_bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    access = Access(config, fake_bot)
    with pytest.raises(UserError, match="I need"):
        await access.require(config.group_id, 99, "can_delete_messages")


async def test_actor_needs_matching_permission(config, fake_bot):
    fake_bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="administrator", can_restrict_members=False))
    access = Access(config, fake_bot)
    with pytest.raises(UserError, match="You need"):
        await access.require(config.group_id, 3, "can_restrict_members")


async def test_network_failure_never_grants_review_rights(config, fake_bot):
    fake_bot.get_chat_member = AsyncMock(side_effect=NetworkError("unavailable"))
    with pytest.raises(UserError, match="administrators"):
        await Access(config, fake_bot).reviewer(1)


async def test_unconfigured_chat_rejected_even_for_owner(config, fake_bot):
    with pytest.raises(UserError, match="not configured"):
        await Access(config, fake_bot).require(-99999, 99)


async def test_group_lock_restores_previous_permissions(service, fake_bot, config):
    original = ChatPermissions(can_send_messages=True, can_send_polls=False, can_invite_users=False)
    fake_bot.get_chat.return_value = SimpleNamespace(permissions=original)
    fake_bot.get_chat.side_effect = None
    mod = Moderation(service, Access(config, fake_bot), fake_bot)
    await mod.lock(config.group_id, False)
    await mod.lock(config.group_id, True)
    restored = fake_bot.set_chat_permissions.call_args.args[1]
    assert restored.can_send_messages is True
    assert restored.can_send_polls is False
    assert restored.can_invite_users is False


async def test_external_member_restrictions_not_overridden(service, fake_bot, config):
    fake_bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="restricted"))
    mod = Moderation(service, Access(config, fake_bot), fake_bot)
    with pytest.raises(UserError, match="outside this bot"):
        await mod.mute(config.group_id, 1, 60)
    fake_bot.restrict_chat_member.assert_not_awaited()


@pytest.mark.parametrize("value", ["0s", "20s", "forever", "99999999d", "1month", "-1h"])
def test_invalid_durations_rejected(value):
    with pytest.raises(UserError):
        duration(value)


def test_duration_accepts_human_units():
    assert duration("30m") == 1800
    assert duration("2h") == 7200
