"""Exercise real PTB serialization and Application dispatch with an offline Bot API transport."""

import json

from telegram import Update
from telegram.ext import Application, ExtBot, TypeHandler
from telegram.request import BaseRequest

from circuit_bot.delivery import DeliveryWorker
from circuit_bot.runtime import Controller


class OfflineTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self.mid = 0

    @property
    def read_timeout(self):
        return 10

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        parameters = request_data.parameters if request_data else {}
        self.calls.append((name, parameters))
        if name == "getMe":
            result = {"id": 500, "is_bot": True, "first_name": "Circuit Perth Bot", "username": "CircuitPerthTestBot"}
        elif name == "sendMessage":
            self.mid += 1
            chat_id = int(parameters["chat_id"])
            result = {"message_id": self.mid, "date": 1700000000,
                      "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                      "text": parameters["text"]}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def test_real_application_report_dispatch_and_outgoing_format(service, db, config):
    transport = OfflineTelegram()
    bot = ExtBot(config.token, request=transport)
    app = Application.builder().bot(bot).updater(None).build()
    controller = Controller(service, bot)
    app.add_handler(TypeHandler(Update, controller.dispatch))
    app.add_error_handler(controller.errors)
    async with app:
        for update_id, text in enumerate(("/report", "@bobuser", "Alice was charged but nothing arrived. Contact alice@example.com."), start=700):
            update = Update.de_json({"update_id": update_id, "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "Alice", "username": "alice"}, "text": text,
            }}, bot)
            await app.process_update(update)
        rows = await db.rows("SELECT * FROM entries")
        assert len(rows) == 1 and rows[0]["status"] == "published"
        worker = DeliveryWorker(db, bot)
        assert await worker.run_once()
        assert not await worker.run_once()
    outgoing = [p for name, p in transport.calls if name == "sendMessage" and int(p["chat_id"]) < 0]
    assert len(outgoing) == 1
    assert {int(p["chat_id"]) for p in outgoing} == {config.channel_id}
    for payload in outgoing:
        assert "Scam report #" in payload["text"] and "Member: @bobuser" in payload["text"]
        assert "alice@example.com" not in payload["text"] and "Alice" not in payload["text"]
        assert "forward_from" not in payload and "entities" not in payload
        assert "parse_mode" not in payload
