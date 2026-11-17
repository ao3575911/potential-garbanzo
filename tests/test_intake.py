import asyncio

from telegram import Update

from circuit_bot.runtime import Intake


def update(bot, uid):
    return Update.de_json({"update_id": uid, "message": {"message_id": uid, "date": 1700000000,
                          "chat": {"id": 1, "type": "private"}, "from": {"id": 1, "is_bot": False, "first_name": "Alice"},
                          "text": "Some private input"}}, bot)


async def test_batch_persists_before_advancing_offset(db, fake_bot):
    intake = Intake(db, fake_bot)
    batch = [update(fake_bot, 123), update(fake_bot, 124)]
    await intake.save_batch(batch)
    await intake.save_batch(batch)
    rows = await db.rows("SELECT * FROM inbox")
    assert len(rows) == 2
    assert all(row["status"] == "queued" for row in rows)
    assert await db.kv("runtime", "offset") == 125


async def test_restart_holds_interrupted_actions_without_replay(db, fake_bot):
    intake = Intake(db, fake_bot)
    await intake.save_batch([update(fake_bot, 123), update(fake_bot, 124)])
    await db.run("UPDATE inbox SET status='processing' WHERE update_id=123")
    await intake.recover()
    row = await db.get("SELECT * FROM inbox WHERE update_id=123")
    assert row["status"] == "held" and row["raw"] == ""
    assert (await db.get("SELECT * FROM inbox WHERE update_id=124"))["status"] == "queued"


async def test_processed_input_body_is_erased(db, fake_bot):
    intake = Intake(db, fake_bot)
    await intake.save_batch([update(fake_bot, 123)])
    stop = asyncio.Event()

    class App:
        async def process_update(self, item):
            assert item.update_id == 123
            stop.set()

    await intake.consume(App(), stop)
    row = await db.get("SELECT * FROM inbox")
    assert row["status"] == "done" and row["raw"] == ""
