import itertools

import pytest
from telegram import Update

from circuit_bot.runtime import Controller
from circuit_bot.service import UserError

IDS = itertools.count(100)


def message(bot, text=None, uid=1, chat_id=None, reply=None, **extra):
    update_id = next(IDS)
    data = {"update_id": update_id, "message": {
        "message_id": update_id, "date": 1700000000,
        "chat": {"id": chat_id or uid, "type": "supergroup" if chat_id and chat_id < 0 else "private"},
        "from": {"id": uid, "is_bot": False, "first_name": "Alice" if uid == 1 else "Carol", "username": "alice" if uid == 1 else "carol"},
        **extra,
    }}
    if text is not None:
        data["message"]["text"] = text
    if reply:
        data["message"]["reply_to_message"] = reply
    return Update.de_json(data, bot)


def callback(bot, data, uid=1, chat_id=None):
    return Update.de_json({"update_id": next(IDS), "callback_query": {
        "id": str(next(IDS)), "chat_instance": "test-chat", "data": data,
        "from": {"id": uid, "is_bot": False, "first_name": "Alice"},
        "message": {"message_id": 60, "date": 1700000000,
                    "chat": {"id": chat_id or uid, "type": "supergroup" if chat_id and chat_id < 0 else "private"}, "text": "Prompt"},
    }}, bot)


@pytest.fixture
def controller(service, fake_bot):
    result = Controller(service, fake_bot)
    result.throttled = lambda *a, **kw: False
    return result


async def test_report_two_answers_publish_immediately(controller, fake_bot, db, config):
    await controller.dispatch(message(fake_bot, "/report"))
    assert "1 of 2" in fake_bot.send_message.call_args.kwargs["text"]
    await controller.dispatch(message(fake_bot, "@unseen_scammer"))
    assert "2 of 2" in fake_bot.send_message.call_args.kwargs["text"]
    await controller.dispatch(message(fake_bot, "I paid for an item and nothing was delivered."))
    entry = await db.get("SELECT * FROM entries")
    assert entry["kind"] == "report" and entry["subject_handle"] == "unseen_scammer"
    assert entry["status"] == "published"
    assert len(await db.rows("SELECT * FROM deliveries")) == 1
    assert await controller.s.session(1) is None
    assert "immediate delivery" in fake_bot.send_message.call_args.kwargs["text"]


async def test_post_has_exact_buttons_and_removes_sender_details(controller, fake_bot, db, config):
    await controller.dispatch(message(fake_bot, "/post"))
    keyboard = fake_bot.send_message.call_args.kwargs["reply_markup"]
    assert [b.text for b in keyboard.inline_keyboard[0]] == ["Bot2Channel", "Bot2Group"]
    await controller.dispatch(callback(fake_bot, keyboard.inline_keyboard[0][1].callback_data))
    await controller.dispatch(message(fake_bot, "Alice can be reached on @alice_user or alice@example.com. I want to discuss this transaction."))
    rows = await db.rows("SELECT * FROM deliveries")
    assert len(rows) == 1 and rows[0]["chat_id"] == config.group_id
    assert "Anonymous chat" in rows[0]["body"]
    assert all(secret not in rows[0]["body"] for secret in ("Alice", "@alice_user", "alice@example.com"))


async def test_vouch_two_answers_queue_channel_and_dossier(controller, fake_bot, db, config):
    await controller.dispatch(message(fake_bot, "/vouch"))
    await controller.dispatch(message(fake_bot, "@bobuser"))
    assert "immediately publish anonymously to the channel" in fake_bot.send_message.call_args.kwargs["text"]
    await controller.dispatch(message(fake_bot, "The item arrived exactly as described. A good experience."))
    entry = await db.get("SELECT * FROM entries")
    assert entry["kind"] == "vouch" and entry["status"] == "published"
    deliveries = await db.rows("SELECT * FROM deliveries")
    assert len(deliveries) == 1 and deliveries[0]["chat_id"] == config.channel_id
    text, _ = await controller.s.dossier(2)
    assert "Vouches: 1" in text


async def test_other_user_cannot_use_destination_button(controller, fake_bot):
    await controller.dispatch(message(fake_bot, "/post"))
    keyboard = fake_bot.send_message.call_args.kwargs["reply_markup"]
    with pytest.raises(UserError, match="expired"):
        await controller.dispatch(callback(fake_bot, keyboard.inline_keyboard[0][0].callback_data, uid=3))


async def test_replaced_post_buttons_are_expired(controller, fake_bot):
    await controller.dispatch(message(fake_bot, "/post"))
    old = fake_bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await controller.dispatch(message(fake_bot, "/report"))
    with pytest.raises(UserError, match="expired"):
        await controller.dispatch(callback(fake_bot, old))


async def test_cancel_prevents_later_message_becoming_submission(controller, fake_bot, db):
    await controller.dispatch(message(fake_bot, "/report"))
    await controller.dispatch(message(fake_bot, "/cancel"))
    await controller.dispatch(message(fake_bot, "This is no longer a scam report."))
    assert not await db.rows("SELECT * FROM entries")


async def test_unknown_command_preserves_form(controller, fake_bot):
    await controller.dispatch(message(fake_bot, "/report"))
    await controller.dispatch(message(fake_bot, "/unknown"))
    assert (await controller.s.session(1))["step"] == "target"


async def test_media_rejected_without_forwarding(controller, fake_bot, db):
    await controller.dispatch(message(fake_bot, "/report"))
    await controller.dispatch(message(fake_bot, "@bobuser"))
    with pytest.raises(UserError, match="text message"):
        await controller.dispatch(message(fake_bot, document={"file_id": "file123", "file_unique_id": "unique123"}))
    assert not await db.rows("SELECT * FROM deliveries")


async def test_self_command_from_group_is_private(controller, fake_bot, config):
    await controller.dispatch(message(fake_bot, "/self", chat_id=config.group_id))
    calls = [call.kwargs for call in fake_bot.send_message.call_args_list]
    assert any(call.get("chat_id") == 1 and "Member dossier" in call["text"] for call in calls)
    assert all("Member dossier" not in call.get("text", "") for call in calls if call.get("chat_id") == config.group_id)


async def test_edited_commands_do_not_create_forms(controller, fake_bot):
    raw = message(fake_bot, "/report").to_dict()
    raw["edited_message"] = raw.pop("message")
    raw["edited_message"]["edit_date"] = 1700000100
    await controller.dispatch(Update.de_json(raw, fake_bot))
    assert await controller.s.session(1) is None


async def test_username_reassignment_does_not_transfer_id_dossier(controller, fake_bot, db):
    await controller.dispatch(message(fake_bot, "Hello, my new username is Alice.", uid=3))
    from telegram import User

    await db.observe(User(3, "Other owner", False, username="alice"))
    assert (await db.get("SELECT username FROM users WHERE id=1"))["username"] is None
    assert (await controller.ui.subject("@alice"))["target_id"] == 3


async def test_admin_action_rejected_for_public_member(controller, fake_bot, config):
    with pytest.raises(UserError, match="administrator permission"):
        await controller.dispatch(message(fake_bot, "/ban 2", chat_id=config.group_id))
    fake_bot.ban_chat_member.assert_not_awaited()


async def test_private_admin_context_does_not_grant_channel_privilege(controller, fake_bot):
    await controller.db.set_kv("admin_context", "1", controller.config.channel_id)
    with pytest.raises(UserError, match="administrator permission"):
        await controller.dispatch(message(fake_bot, "/settitle Unauthorised change"))
    fake_bot.set_chat_title.assert_not_awaited()


async def test_group_report_command_redirects_to_private(controller, fake_bot, config):
    await controller.dispatch(message(fake_bot, "/report @bobuser", chat_id=config.group_id))
    assert "Continue in private" in fake_bot.send_message.call_args.kwargs["text"]
    assert await controller.s.session(1) is None
