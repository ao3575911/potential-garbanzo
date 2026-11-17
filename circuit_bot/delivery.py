from __future__ import annotations

import asyncio
import logging

from telegram import LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .db import Database, one
from .service import enqueue, now

log = logging.getLogger(__name__)


class DeliveryWorker:
    """Durable delivery; uncertain sends are held for owner review, never blindly replayed."""

    def __init__(self, db: Database, bot, owners=()):
        self.db, self.bot = db, bot
        self.owners = owners

    async def recover(self):
        await self.db.run("UPDATE deliveries SET status='uncertain',note='Restart during send; check destination before retrying.' WHERE status='sending'")

    async def run_once(self) -> bool:
        async with self.db.tx() as conn:
            item = await one(conn, "SELECT * FROM deliveries WHERE status='queued' AND next_attempt<=? ORDER BY id LIMIT 1", (now(),))
            if not item:
                return False
            await conn.execute("UPDATE deliveries SET status='sending',attempts=attempts+1 WHERE id=?", (item["id"],))
        try:
            sent = await self.bot.send_message(
                chat_id=item["chat_id"], text=item["body"], parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                connect_timeout=10, read_timeout=20, write_timeout=20,
            )
        except RetryAfter as exc:
            wait = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else exc.retry_after
            await self.db.run("UPDATE deliveries SET status='queued',next_attempt=?,note='Telegram rate limit; waiting.' WHERE id=?",
                              (now() + int(wait) + 1, item["id"]))
        except (Forbidden, BadRequest):
            await self.db.run("UPDATE deliveries SET status='failed',note='Telegram rejected delivery. Check bot permissions and destination.' WHERE id=?", (item["id"],))
        except NetworkError:
            await self.uncertain(item["id"], "Network interrupted. Check destination before retrying.")
        except asyncio.CancelledError:
            await self.uncertain(item["id"], "Shutdown during delivery. Check destination before retrying.")
            raise
        except Exception as exc:
            log.error("delivery_error id=%s type=%s", item["id"], type(exc).__name__)
            await self.uncertain(item["id"], "Delivery outcome unknown. Check destination before retrying.")
        else:
            await self.db.run("UPDATE deliveries SET status='sent',message_id=?,note='' WHERE id=?", (sent.message_id, item["id"]))
            # A withdraw/reject may race a send. Remove confirmed outgoing message if closed meanwhile.
            if item["entry_id"]:
                entry = await self.db.get("SELECT status,kind FROM entries WHERE id=?", (item["entry_id"],))
                if entry and entry["status"] in {"withdrawn", "rejected", "resolved"}:
                    try:
                        if entry["kind"] in {"report", "vouch"}:
                            label = "Scam report" if entry["kind"] == "report" else "Vouch"
                            await self.bot.edit_message_text(chat_id=item["chat_id"], message_id=sent.message_id,
                                                             text=f"{label} #{item['entry_id']} · {entry['status']}\nCheck the member’s dossier for current records.", parse_mode=None)
                        else:
                            await self.bot.delete_message(item["chat_id"], sent.message_id)
                        await self.db.run("UPDATE deliveries SET status='canceled',note='Removed after withdrawal or rejection.' WHERE id=?", (item["id"],))
                    except Exception:
                        await self.db.run("UPDATE deliveries SET note='Closed submission remains visible; owner must remove it.' WHERE id=?", (item["id"],))
        state = await self.db.get("SELECT status FROM deliveries WHERE id=?", (item["id"],))
        if state["status"] in {"failed", "uncertain"} and not item["dedupe_key"].startswith("notice:"):
            async with self.db.tx() as conn:
                for owner_id in self.owners:
                    await enqueue(conn, f"notice:{item['id']}:{item['attempts']}:{owner_id}", owner_id,
                                  f"Circuit Perth · Delivery #{item['id']} is {state['status']}.\nCheck /deliveries before retrying. An uncertain send may already be visible.")
        return True

    async def uncertain(self, delivery_id: int, note: str):
        await self.db.run("UPDATE deliveries SET status='uncertain',note=? WHERE id=?", (note, delivery_id))
