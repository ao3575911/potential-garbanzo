from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions, User

from circuit_bot.config import Config
from circuit_bot.db import Database
from circuit_bot.service import Service


@pytest.fixture
def config(tmp_path):
    return Config(token="123456:" + "x" * 32, owners=frozenset({99}), group_id=-1001,
                  channel_id=-1002, data_dir=tmp_path)


@pytest.fixture
async def db(config):
    value = Database(config.db_path)
    await value.open()
    for uid, username in ((1, "alice"), (2, "bobuser"), (3, "carol"), (99, "owner")):
        await value.observe(User(uid, username.title(), False, username=username))
    yield value
    await value.close()


@pytest.fixture
def service(db, config):
    return Service(db, config)


@pytest.fixture
def fake_bot(config):
    bot = SimpleNamespace(id=500, username="CircuitPerthTestBot")
    for method in ("send_message", "edit_message_text", "answer_callback_query", "delete_message", "delete_messages", "restrict_chat_member", "ban_chat_member", "unban_chat_member", "set_chat_permissions", "pin_chat_message", "unpin_chat_message", "set_chat_title", "set_chat_description", "promote_chat_member", "get_chat_administrators", "get_updates", "set_my_commands", "approve_chat_join_request", "decline_chat_join_request"):
        setattr(bot, method, AsyncMock())
    bot.send_message.return_value = SimpleNamespace(message_id=101)

    async def member(chat_id, user_id):
        status = "administrator" if user_id in {99, 500} else "member"
        return SimpleNamespace(status=status, user=User(user_id, "Member", False), can_delete_messages=True,
                               can_restrict_members=True, can_change_info=True, can_pin_messages=True,
                               can_post_messages=True, can_edit_messages=True, can_invite_users=True,
                               can_promote_members=True)

    async def chat(chat_id):
        return SimpleNamespace(id=chat_id, type="channel" if chat_id == config.channel_id else "supergroup",
                               permissions=ChatPermissions.all_permissions())

    bot.get_chat_member = AsyncMock(side_effect=member)
    bot.get_chat = AsyncMock(side_effect=chat)
    return bot
