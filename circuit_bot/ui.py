from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import Forbidden, TelegramError

from .service import Service, UserError

HANDLE = re.compile(r"@?([a-z][a-z0-9_]{3,31})", re.I)


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=value) for label, value in row] for row in rows])


class UI:
    def __init__(self, service: Service, bot):
        self.s, self.db, self.bot, self.config = service, service.db, bot, service.config

    async def say(self, update, text: str, keyboard=None):
        await self.send_to(update.effective_chat.id, text, keyboard)

    async def send_to(self, chat_id: int, text: str, keyboard=None):
        # Telegram limits UTF-16 code units. Split even emoji-heavy dossiers safely.
        chunks, current, units = [], "", 0
        for character in text:
            size = 2 if ord(character) > 0xFFFF else 1
            if units + size > 3900:
                chunks.append(current)
                current, units = "", 0
            current += character
            units += size
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks):
            await self.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=None,
                                        reply_markup=keyboard if index == len(chunks) - 1 else None,
                                        link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def private(self, update, payload: str = "start") -> bool:
        if update.effective_chat.type == "private":
            return True
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Open Circuit Perth Bot", url=f"https://t.me/{self.bot.username}?start={payload}")]])
        await self.say(update, "Continue in private chat with Circuit Perth Bot.", keyboard)
        return False

    async def subject(self, text: str = "", reply=None, allow_unknown: bool = True) -> dict:
        if reply and reply.from_user and not reply.sender_chat and not reply.from_user.is_bot:
            await self.db.observe(reply.from_user, self.config.group_id)
            return {"target_id": reply.from_user.id, "subject_handle": reply.from_user.username}
        text = text.strip()
        if text.isdigit():
            user = await self.db.get("SELECT * FROM users WHERE id=?", (int(text),))
            if not user:
                raise UserError("I have not seen that user ID. Reply to their group message or use their @username.")
            return {"target_id": user["id"], "subject_handle": user["username"]}
        match = HANDLE.fullmatch(text)
        if not match:
            raise UserError("Enter a valid @username, a known numeric user ID, or reply to a member’s message.")
        handle = match[1].lower()
        user = await self.db.get("SELECT * FROM users WHERE username=? COLLATE NOCASE", (handle,))
        if user:
            return {"target_id": user["id"], "subject_handle": handle}
        if not allow_unknown:
            raise UserError("I cannot identify that username. Reply to their group message, or ask them to /start the bot.")
        return {"target_id": None, "subject_handle": handle}

    async def show_dossier(self, update, subject: dict, page: int = 0, dm=False):
        if subject.get("target_id"):
            key = str(subject["target_id"])
            text, pages = await self.s.dossier(subject["target_id"], page)
        else:
            key = "h_" + subject["subject_handle"]
            text, pages = await self.s.handle_dossier(subject["subject_handle"], page)
        page = max(0, min(page, pages - 1))
        nav = []
        if page > 0:
            nav.append(("Previous", f"dos:{update.effective_user.id}:{key}:{page - 1}"))
        if page + 1 < pages:
            nav.append(("Next", f"dos:{update.effective_user.id}:{key}:{page + 1}"))
        keyboard_rows = [nav] if nav else []
        if subject.get("target_id"):
            profile = await self.db.get("SELECT username FROM users WHERE id=?", (subject["target_id"],))
            handle = profile["username"] if profile else None
            if handle:
                unverified = await self.db.get("SELECT COUNT(*) n FROM entries WHERE target_id IS NULL AND subject_handle=? COLLATE NOCASE AND status IN ('published','resolved')", (handle,))
                if unverified["n"]:
                    keyboard_rows.append([("Unverified handle records", f"dos:{update.effective_user.id}:h_{handle.lower()}:0")])
        keyboard = buttons(keyboard_rows) if keyboard_rows else None
        if dm:
            try:
                await self.send_to(update.effective_user.id, text, keyboard)
            except Forbidden:
                await self.private(update, "self")
                return
            if update.effective_chat.type != "private":
                await self.say(update, "Your dossier has been sent to your private chat.")
        elif update.callback_query and len(text.encode('utf-16-le')) <= 7800:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=None,
                                                          link_preview_options=LinkPreviewOptions(is_disabled=True))
        else:
            await self.say(update, text, keyboard)

    async def close_public(self, entry: dict, state: str) -> int:
        """Update or remove already delivered public content. Returns failures for follow-up."""
        deliveries = await self.db.rows("SELECT * FROM deliveries WHERE entry_id=? AND status='sent'", (entry["id"],))
        failures = 0
        for item in deliveries:
            try:
                if entry["kind"] in {"report", "vouch"}:
                    label = "Scam report" if entry["kind"] == "report" else "Vouch"
                    await self.bot.edit_message_text(chat_id=item["chat_id"], message_id=item["message_id"],
                                                     text=f"{label} #{entry['id']} · {state}\nThis submission is no longer active. Check the member’s dossier for current records.", parse_mode=None)
                else:
                    await self.bot.delete_message(item["chat_id"], item["message_id"])
                await self.db.run("UPDATE deliveries SET status='canceled',note=? WHERE id=?", (state, item["id"]))
            except TelegramError:
                failures += 1
                await self.db.run("UPDATE deliveries SET note='Closed submission remains visible; manual action required.' WHERE id=?", (item["id"],))
        uncertain = await self.db.get("SELECT COUNT(*) n FROM deliveries WHERE entry_id=? AND status IN ('uncertain','sending')", (entry["id"],))
        return failures + uncertain["n"]
