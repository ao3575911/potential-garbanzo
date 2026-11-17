from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from .access import ADMIN_STATUSES, Access
from .privacy import has_link, normalize
from .service import Service, UserError, now

log = logging.getLogger(__name__)


def duration(value: str, maximum: int = 31_536_000) -> int:
    match = re.fullmatch(r"([1-9]\d{0,7})(s|m|h|d|w)", value.lower())
    if not match:
        raise UserError("Use a duration such as 30m, 2h or 7d.")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    if not 60 <= seconds <= maximum:
        raise UserError("Duration must be between 1 minute and 365 days.")
    return seconds


def permission_dict(permissions) -> dict:
    return permissions.to_dict() if permissions else {"can_send_messages": True}


class Moderation:
    def __init__(self, service: Service, access: Access, bot):
        self.s, self.db, self.access, self.bot = service, service.db, access, bot
        self.recent: dict[int, deque] = defaultdict(deque)
        self.admin_cache: dict[int, tuple[float, bool]] = {}

    async def mute(self, chat_id: int, target: int, seconds: int):
        await self.access.protect_target(chat_id, target)
        member = await self.bot.get_chat_member(chat_id, target)
        key = f"mute:{chat_id}:{target}"
        saved = await self.db.kv("moderation", key)
        if member.status == ChatMemberStatus.RESTRICTED and not saved:
            raise UserError("This member already has restrictions set outside this bot. Manage them in Telegram.")
        if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
            raise UserError("This member is not currently in the group.")
        chat = await self.bot.get_chat(chat_id)
        if not saved:
            await self.db.set_kv("moderation", key, permission_dict(chat.permissions))
        await self.bot.restrict_chat_member(chat_id, target, ChatPermissions(can_send_messages=False),
                                            until_date=datetime.now(UTC) + timedelta(seconds=seconds))

    async def unmute(self, chat_id: int, target: int):
        await self.access.protect_target(chat_id, target)
        key = f"mute:{chat_id}:{target}"
        saved = await self.db.kv("moderation", key)
        if not saved:
            raise UserError("There is no mute from this bot to remove. Manage other restrictions in Telegram.")
        chat = await self.bot.get_chat(chat_id)
        defaults = permission_dict(chat.permissions)
        permissions = {k: bool(v and defaults.get(k, False)) for k, v in saved.items()}
        await self.bot.restrict_chat_member(chat_id, target, ChatPermissions.de_json(permissions, self.bot),
                                            use_independent_chat_permissions=True)
        await self.db.run("DELETE FROM kv WHERE scope='moderation' AND key=?", (key,))

    async def warn(self, chat_id: int, target: int, actor: int, reason: str) -> str:
        count = await self.s.warning(chat_id, target, actor, reason)
        settings = await self.s.settings(chat_id)
        result = f"Warning {count}/{settings['warn_limit']} recorded for member {target}."
        if count >= settings["warn_limit"]:
            await self.mute(chat_id, target, settings["mute_minutes"] * 60)
            result += f" Muted for {settings['mute_minutes']} minutes."
        return result

    async def lock(self, chat_id: int, unlock: bool):
        saved = await self.db.kv(str(chat_id), "lock_permissions")
        if unlock:
            if not saved:
                raise UserError("This bot has not locked the group.")
            await self.bot.set_chat_permissions(chat_id, ChatPermissions.de_json(saved, self.bot), use_independent_chat_permissions=True)
            await self.db.run("DELETE FROM kv WHERE scope=? AND key='lock_permissions'", (str(chat_id),))
        else:
            if saved:
                raise UserError("The group is already locked by this bot.")
            chat = await self.bot.get_chat(chat_id)
            await self.db.set_kv(str(chat_id), "lock_permissions", permission_dict(chat.permissions))
            await self.bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=False))

    async def inspect(self, message) -> bool:
        """Apply group rules before commands as well as ordinary messages."""
        user = message.from_user
        if message.chat_id != self.s.config.group_id or not user or user.is_bot or message.sender_chat:
            return False
        cached = self.admin_cache.get(user.id)
        if not cached or cached[0] < time.monotonic() - 20:
            is_admin = await self.access.is_admin(message.chat_id, user.id)
            self.admin_cache[user.id] = (time.monotonic(), is_admin)
        if self.admin_cache[user.id][1]:
            return False
        settings = await self.s.settings(message.chat_id)
        reason = None
        queue = self.recent[user.id]
        current = time.monotonic()
        while queue and queue[0] < current - settings["flood_seconds"]:
            queue.popleft()
        # Edits are rescanned for links/words, but are not extra flood events.
        if not message.edit_date:
            queue.append(current)
        if settings["antiflood"] and len(queue) > settings["flood_count"]:
            reason = "Message flood"
        text = normalize(message.text or message.caption or "")
        entities = message.entities or message.caption_entities
        if settings["antilink"] and has_link(text, entities):
            reason = "Links are disabled"
        if settings["block_media"] and (message.photo or message.video or message.document or message.sticker or message.voice or message.audio or message.animation or message.video_note):
            reason = "Media is disabled"
        for word in settings["banned_words"]:
            if word.casefold() in text.casefold():
                reason = "Blocked word or phrase"
                break
        hour = datetime.now(ZoneInfo(self.s.config.timezone)).hour
        start, end = settings["quiet_start"], settings["quiet_end"]
        if start >= 0 and end >= 0 and (start <= hour < end if start < end else hour >= start or hour < end):
            reason = "Quiet hours are active"
        if not reason:
            return False
        # Recheck before any destructive action; a cached role is insufficient.
        if await self.access.is_admin(message.chat_id, user.id):
            return False
        try:
            await self.access.protect_target(message.chat_id, user.id)
        except UserError:
            return False
        await self.access.require(message.chat_id, self.bot.id, "can_delete_messages")
        await self.bot.delete_message(message.chat_id, message.message_id)
        await self.db.audit(self.bot.id, "auto_delete", str(user.id), {"reason": reason, "chat_id": message.chat_id})
        if reason == "Message flood":
            try:
                await self.mute(message.chat_id, user.id, 60)
            except UserError:
                pass
            queue.clear()
        return True

    async def joined(self, chat_id: int, user):
        if chat_id != self.s.config.group_id or user.is_bot:
            return
        await self.db.observe(user, chat_id)
        settings = await self.s.settings(chat_id)
        if settings["captcha"]:
            await self.create_captcha(chat_id, user)
        elif settings["welcome"]:
            await self.bot.send_message(chat_id, settings["welcome"].replace("{name}", user.first_name), parse_mode=None)

    async def create_captcha(self, chat_id: int, user):
        member = await self.bot.get_chat_member(chat_id, user.id)
        if member.status in ADMIN_STATUSES or member.status == ChatMemberStatus.RESTRICTED:
            return
        chat = await self.bot.get_chat(chat_id)
        a, b = secrets.randbelow(8) + 1, secrets.randbelow(8) + 1
        answer = a + b
        choices = list({answer, answer + 1, answer + 2})
        secrets.SystemRandom().shuffle(choices)
        nonce = secrets.token_hex(4)
        await self.db.run("INSERT OR REPLACE INTO captchas VALUES(?,?,?,?,?,?,'creating',NULL)",
                          (chat_id, user.id, str(answer), nonce, now() + 180, json.dumps(permission_dict(chat.permissions))))
        # No until_date: the timeout worker removes failed joins; no auto-release before verification.
        await self.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
        await self.db.run("UPDATE captchas SET status='pending' WHERE chat_id=? AND user_id=?", (chat_id, user.id))
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(str(v), callback_data=f"cap:{user.id}:{nonce}:{v}") for v in choices]])
        sent = await self.bot.send_message(chat_id, f"{user.first_name}, verify within 3 minutes: {a} + {b} = ?", reply_markup=keyboard, parse_mode=None)
        await self.db.run("UPDATE captchas SET message_id=? WHERE chat_id=? AND user_id=?", (sent.message_id, chat_id, user.id))

    async def captcha_answer(self, query, data: list[str]):
        if len(data) != 4 or not query.message:
            raise UserError("Verification expired. Ask an administrator for help.")
        _, user_id, nonce, answer = data
        if str(query.from_user.id) != user_id:
            raise UserError("This verification belongs to another member.")
        row = await self.db.get("SELECT * FROM captchas WHERE chat_id=? AND user_id=? AND nonce=? AND status='pending'",
                                (query.message.chat_id, query.from_user.id, nonce))
        if not row or row["expires_at"] <= now():
            raise UserError("Verification expired. Ask an administrator for help.")
        if row["answer"] != answer:
            raise UserError("That answer is incorrect. Please try again.")
        await self.release_captcha(query.message.chat_id, query.from_user.id)
        await query.edit_message_text("Verification complete. Welcome. Please read /rules.")

    async def release_captcha(self, chat_id: int, user_id: int):
        row = await self.db.get("SELECT * FROM captchas WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        if not row:
            raise UserError("No join challenge from this bot is recorded for that member.")
        await self.access.protect_target(chat_id, user_id)
        defaults = permission_dict((await self.bot.get_chat(chat_id)).permissions)
        permissions = {k: bool(v and defaults.get(k, False)) for k, v in json.loads(row["permissions"]).items()}
        await self.bot.restrict_chat_member(chat_id, user_id,
                                            ChatPermissions.de_json(permissions, self.bot), use_independent_chat_permissions=True)
        await self.db.run("DELETE FROM captchas WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def expire_captchas(self):
        rows = await self.db.rows("SELECT * FROM captchas WHERE expires_at<=? AND status IN ('pending','creating')", (now(),))
        for row in rows:
            try:
                await self.access.protect_target(row["chat_id"], row["user_id"])
                member = await self.bot.get_chat_member(row["chat_id"], row["user_id"])
                if member.status != ChatMemberStatus.RESTRICTED or getattr(member, "can_send_messages", False):
                    await self.db.run("DELETE FROM captchas WHERE chat_id=? AND user_id=?", (row["chat_id"], row["user_id"]))
                    continue
                await self.db.run("UPDATE captchas SET status='expiring' WHERE chat_id=? AND user_id=?", (row["chat_id"], row["user_id"]))
                # One bounded ban request avoids an unsafe ban/unban pair on network interruption.
                await self.bot.ban_chat_member(row["chat_id"], row["user_id"], until_date=datetime.now(UTC) + timedelta(seconds=60), revoke_messages=False)
                await self.db.run("DELETE FROM captchas WHERE chat_id=? AND user_id=?", (row["chat_id"], row["user_id"]))
                if row["message_id"]:
                    try:
                        await self.bot.delete_message(row["chat_id"], row["message_id"])
                    except TelegramError:
                        pass
            except UserError:
                # A promoted member no longer needs join protection.
                await self.db.run("DELETE FROM captchas WHERE chat_id=? AND user_id=?", (row["chat_id"], row["user_id"]))
            except TelegramError as exc:
                await self.db.run("UPDATE captchas SET status='held' WHERE chat_id=? AND user_id=?", (row["chat_id"], row["user_id"]))
                log.error("captcha_cleanup type=%s", type(exc).__name__)

    def prune(self):
        cutoff = time.monotonic() - 120
        self.recent = defaultdict(deque, {k: v for k, v in self.recent.items() if v and v[-1] >= cutoff})
        self.admin_cache = {k: v for k, v in self.admin_cache.items() if v[0] >= cutoff}
