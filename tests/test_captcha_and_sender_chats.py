import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions, User
from telegram.error import TimedOut

from circuit_bot.access import Access
from circuit_bot.moderation import Moderation
from circuit_bot.runtime import Controller
from circuit_bot.service import UserError
from tests.test_flows import callback, message


async def test_captcha_only_named_member_can_answer(service, config, fake_bot, db):
    mod = Moderation(service, Access(config, fake_bot), fake_bot)
    await mod.create_captcha(config.group_id, User(1, "Alice", False))
    row = await db.get("SELECT * FROM captchas")
    query = callback(fake_bot, f"cap:1:{row['nonce']}:{row['answer']}", uid=3, chat_id=config.group_id).callback_query
    with pytest.raises(UserError, match="another member"):
        await mod.captcha_answer(query, query.data.split(":"))
    assert (await db.get("SELECT status FROM captchas"))["status"] == "pending"


async def test_captcha_correct_answer_restores_captured_permissions(service, config, fake_bot, db):
    mod = Moderation(service, Access(config, fake_bot), fake_bot)
    await mod.create_captcha(config.group_id, User(1, "Alice", False))
    row = await db.get("SELECT * FROM captchas")
    query = callback(fake_bot, f"cap:1:{row['nonce']}:{row['answer']}", chat_id=config.group_id).callback_query
    await mod.captcha_answer(query, query.data.split(":"))
    assert await db.get("SELECT * FROM captchas") is None
    assert fake_bot.restrict_chat_member.call_args.args[2].can_send_messages is True


async def test_captcha_timeout_network_failure_is_held(service, config, fake_bot, db):
    await db.run("INSERT INTO captchas VALUES(?,1,'4','nonce',0,?,'pending',NULL)", (config.group_id, json.dumps(ChatPermissions.all_permissions().to_dict())))
    fake_bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="restricted", can_send_messages=False))
    fake_bot.ban_chat_member.side_effect = TimedOut()
    mod = Moderation(service, Access(config, fake_bot), fake_bot)
    await mod.expire_captchas()
    await mod.expire_captchas()
    assert (await db.get("SELECT status FROM captchas"))["status"] == "held"
    assert fake_bot.ban_chat_member.await_count == 1
    fake_bot.unban_chat_member.assert_not_awaited()


async def test_external_channel_senders_cannot_bypass_moderation(service, config, fake_bot):
    controller = Controller(service, fake_bot)
    await controller.dispatch(message(fake_bot, "Spam as a channel", chat_id=config.group_id,
                                      sender_chat={"id": -888, "type": "channel", "title": "External sender"}))
    fake_bot.delete_message.assert_awaited_once()


async def test_linked_channel_automatic_forward_is_preserved(service, config, fake_bot):
    controller = Controller(service, fake_bot)
    await controller.dispatch(message(fake_bot, "Our own channel post", chat_id=config.group_id,
                                      sender_chat={"id": config.channel_id, "type": "channel", "title": "Circuit"},
                                      is_automatic_forward=True))
    fake_bot.delete_message.assert_not_awaited()
