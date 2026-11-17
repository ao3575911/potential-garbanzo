from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from circuit_bot.delivery import DeliveryWorker
from circuit_bot.service import enqueue, now


async def queue(db, config):
    async with db.tx() as conn:
        await enqueue(conn, "test-send", config.channel_id, "Anonymous chat\n\nAn anonymous message.")


async def test_success_records_message_without_replay(db, config):
    await queue(db, config)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    worker = DeliveryWorker(db, bot)
    assert await worker.run_once()
    assert not await worker.run_once()
    assert bot.send_message.await_count == 1
    row = await db.get("SELECT * FROM deliveries")
    assert row["status"] == "sent" and row["message_id"] == 42
    assert bot.send_message.call_args.kwargs["parse_mode"] is None


async def test_timeout_held_instead_of_duplicate(db, config):
    await queue(db, config)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
    worker = DeliveryWorker(db, bot)
    await worker.run_once()
    assert not await worker.run_once()
    assert bot.send_message.await_count == 1
    assert (await db.get("SELECT status FROM deliveries"))["status"] == "uncertain"


async def test_telegram_rate_limit_requeues_after_delay(db, config):
    await queue(db, config)
    worker = DeliveryWorker(db, SimpleNamespace(send_message=AsyncMock(side_effect=RetryAfter(30))))
    await worker.run_once()
    row = await db.get("SELECT * FROM deliveries")
    assert row["status"] == "queued" and row["next_attempt"] >= now() + 30
    assert not await worker.run_once()


@pytest.mark.parametrize("error", [Forbidden("denied"), BadRequest("chat not found")])
async def test_permission_failures_are_visible_and_do_not_retry(db, config, error):
    await queue(db, config)
    worker = DeliveryWorker(db, SimpleNamespace(send_message=AsyncMock(side_effect=error)))
    await worker.run_once()
    assert (await db.get("SELECT status FROM deliveries"))["status"] == "failed"
    assert not await worker.run_once()


async def test_restart_holds_inflight_but_preserves_queued(db, config):
    await queue(db, config)
    await db.run("UPDATE deliveries SET status='sending'")
    worker = DeliveryWorker(db, None)
    await worker.recover()
    assert (await db.get("SELECT status FROM deliveries"))["status"] == "uncertain"


async def test_owner_failure_notices_do_not_create_notification_loop(db, config):
    await queue(db, config)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("denied")))
    worker = DeliveryWorker(db, bot, owners={99})
    await worker.run_once()
    rows = await db.rows("SELECT * FROM deliveries")
    assert len(rows) == 2 and rows[1]["chat_id"] == 99 and rows[1]["dedupe_key"].startswith("notice:")
    await worker.run_once()
    assert len(await db.rows("SELECT * FROM deliveries")) == 2
    assert not await worker.run_once()


async def test_closed_entry_cannot_leak_in_send_withdraw_race(service, db, config, fake_bot):
    nonce = await service.begin(1, "post", "preview", {"body": "A useful anonymous message.", "destination": config.channel_id})
    entry, _ = await service.submit(1, nonce)

    async def send_and_withdraw(**kwargs):
        await service.withdraw(entry["id"], 1)
        return SimpleNamespace(message_id=88)

    fake_bot.send_message.side_effect = send_and_withdraw
    await DeliveryWorker(db, fake_bot).run_once()
    fake_bot.delete_message.assert_awaited_once_with(config.channel_id, 88)
    assert (await db.get("SELECT status FROM deliveries"))["status"] == "canceled"
