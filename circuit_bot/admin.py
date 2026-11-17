from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from telegram.error import TelegramError

from .moderation import duration
from .service import UserError, audit, now
from .ui import buttons

ADMIN_HELP = """Circuit Perth · Administration

/admin group or /admin channel — select a chat in private
/settings — toggle group protections
/set KEY VALUE — change a setting
/filter add PHRASE · /filter remove PHRASE
/filters — list blocked phrases

/ban USER [DURATION] · /unban USER · /kick USER
/mute USER DURATION · /unmute USER
/warn USER REASON · /unwarn USER · /warnings USER
/del — delete a replied-to message
/purge — delete from a replied-to message through this command (max 100)
/pin · /unpin — reply to a message
/lock · /unlock — group writing permissions
/settitle TEXT · /setdescription TEXT
/invite — create a one-use, one-hour invite
/revokeinvite LINK — revoke a bot-created invite
/admins — list administrators

/queue — submissions awaiting review
/review ID — inspect a submission privately
/approve ID · /reject ID REASON · /resolve ID REASON
/joinrequests · /approvejoin USER_ID · /declinejoin USER_ID
/captchas · /verify USER — inspect or release a join challenge
/stats — aggregate counts

Owner, private chat:
/schedule group|channel DURATION TEXT
/repeat group|channel DURATION TEXT
/timers · /canceltimer ID
/setcommand NAME RESPONSE · /delcommand NAME
/autoreply KEYWORD | RESPONSE · /delreply KEYWORD
/promote USER · /demote USER — limited staff rights
/deliveries [PAGE] · /retry DELIVERY_ID · /canceldelivery ID
/audit [PAGE] · /inbox · /health

USER can be a known @username, user ID, or a replied-to member. Telegram administrator permissions are checked for each action."""

MOD_COMMANDS = {"ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "warnings"}
REVIEW_COMMANDS = {"queue", "review", "approve", "reject", "resolve"}
CHAT_COMMANDS = {"del", "purge", "pin", "unpin", "lock", "unlock", "settitle", "setdescription", "invite", "revokeinvite", "admins", "promote", "demote"}
OWNER_COMMANDS = {"schedule", "repeat", "timers", "canceltimer", "setcommand", "delcommand", "autoreply", "delreply", "deliveries", "retry", "canceldelivery", "audit", "inbox", "health"}
PUBLIC_COMMANDS = {"start", "help", "id", "privacy", "rules", "cancel", "vouch", "report", "check", "self", "handle", "post", "mysubmissions", "status", "withdraw", "appeal"}
RESERVED = PUBLIC_COMMANDS | MOD_COMMANDS | REVIEW_COMMANDS | CHAT_COMMANDS | OWNER_COMMANDS | {"admin", "settings", "set", "filter", "filters", "stats", "joinrequests", "approvejoin", "declinejoin", "captchas", "verify"}


class Admin:
    def __init__(self, ui, access, moderation):
        self.ui, self.s, self.db, self.config = ui, ui.s, ui.db, ui.config
        self.access, self.mod, self.bot = access, moderation, ui.bot

    async def chat(self, update) -> int:
        if update.effective_chat.type != "private":
            return update.effective_chat.id
        return await self.db.kv("admin_context", str(update.effective_user.id), self.config.group_id)

    async def command(self, name: str, update, args: list[str]) -> bool:
        if name not in RESERVED - PUBLIC_COMMANDS:
            return False
        uid = update.effective_user.id
        chat_id = await self.chat(update)
        if name == "admin":
            await self.access.reviewer(uid)
            if args:
                if update.effective_chat.type != "private" or args[0] not in {"group", "channel"}:
                    raise UserError("Use /admin group or /admin channel in private chat.")
                chat_id = self.config.group_id if args[0] == "group" else self.config.channel_id
                await self.access.require(chat_id, uid)
                await self.db.set_kv("admin_context", str(uid), chat_id)
            await self.ui.say(update, f"Selected chat: {'group' if chat_id == self.config.group_id else 'channel'} ({chat_id})\n\n" + ADMIN_HELP)
        elif name in REVIEW_COMMANDS:
            if await self.ui.private(update):
                await self.access.reviewer(uid)
                await self.review_command(name, update, args)
        elif name in OWNER_COMMANDS:
            if await self.ui.private(update):
                self.access.owner(uid)
                await self.owner_command(name, update, args, chat_id)
        elif name in MOD_COMMANDS:
            await self.member_action(name, update, args, chat_id)
        elif name in CHAT_COMMANDS:
            await self.chat_action(name, update, args, chat_id)
        elif name in {"settings", "set", "filter", "filters"}:
            if chat_id != self.config.group_id:
                raise UserError("Select /admin group to change group protections.")
            await self.access.require(chat_id, uid, "can_change_info")
            if name == "filter" or (name == "set" and args and args[0] in {"quiet_start", "quiet_end"}):
                await self.access.require(chat_id, uid, "can_delete_messages")
            await self.settings_command(name, update, args, chat_id)
        elif name in {"captchas", "verify"}:
            await self.access.require(chat_id, uid, "can_restrict_members")
            if chat_id != self.config.group_id:
                raise UserError("Join challenges apply to the group. Select /admin group.")
            if name == "captchas":
                rows = await self.db.rows("SELECT user_id,status FROM captchas WHERE chat_id=? LIMIT 20", (chat_id,))
                await self.ui.say(update, "Join challenges\n" + ("\n".join(f"{r['user_id']} · {r['status']}" for r in rows) or "None.") + "\n\n/verify USER_ID releases a bot-created challenge.")
            else:
                target = self.number(args, "/verify USER_ID")
                await self.mod.release_captcha(chat_id, target)
                await self.db.audit(uid, "verify_join", str(target), {"chat_id": chat_id})
                await self.ui.say(update, "Join challenge released.")
        elif name in {"joinrequests", "approvejoin", "declinejoin"}:
            await self.access.require(chat_id, uid, "can_invite_users")
            if name == "joinrequests":
                rows = await self.db.rows("SELECT user_id FROM join_requests WHERE chat_id=? ORDER BY created_at LIMIT 20", (chat_id,))
                await self.ui.say(update, "Pending join requests\n" + ("\n".join(str(r["user_id"]) for r in rows) or "None recorded."))
            else:
                target = self.number(args, f"/{name} USER_ID")
                if name == "approvejoin":
                    await self.bot.approve_chat_join_request(chat_id, target)
                else:
                    await self.bot.decline_chat_join_request(chat_id, target)
                await self.db.run("DELETE FROM join_requests WHERE chat_id=? AND user_id=?", (chat_id, target))
                await self.db.audit(uid, name, str(target), {"chat_id": chat_id})
                await self.ui.say(update, "Join request updated.")
        elif name == "stats":
            await self.access.reviewer(uid)
            rows = await self.db.rows("SELECT kind,status,COUNT(*) n FROM entries GROUP BY kind,status")
            members = await self.db.get("SELECT COUNT(*) n FROM members WHERE chat_id=?", (self.config.group_id,))
            await self.ui.say(update, "Circuit Perth · Activity\n" + f"Group members observed: {members['n']}\n" + "\n".join(f"{r['kind']} · {r['status']}: {r['n']}" for r in rows))
        return True

    @staticmethod
    def number(args, usage):
        if not args or not args[0].isdigit() or int(args[0]) <= 0:
            raise UserError("Use " + usage + ".")
        return int(args[0])

    async def target(self, update, args):
        reply = update.effective_message.reply_to_message
        if reply and reply.from_user and not reply.sender_chat and not reply.from_user.is_bot:
            subject = await self.ui.subject(reply=reply, allow_unknown=False)
            return subject["target_id"], args
        if not args:
            raise UserError("Reply to a member’s message or include their known @username or Telegram ID.")
        # A numeric ID is usable for unbanning even if the bot has no saved profile.
        if args[0].isdigit() and int(args[0]) > 0:
            return int(args[0]), args[1:]
        subject = await self.ui.subject(args[0], allow_unknown=False)
        return subject["target_id"], args[1:]

    async def member_action(self, name, update, args, chat_id):
        uid = update.effective_user.id
        await self.access.require(chat_id, uid, "can_restrict_members")
        if chat_id == self.config.channel_id and name in {"mute", "unmute", "warn", "unwarn", "warnings"}:
            raise UserError("This action is available in the group. Select /admin group.")
        target, rest = await self.target(update, args)
        if name != "warnings":
            await self.access.protect_target(chat_id, target)
        if name == "ban":
            until = datetime.now(UTC) + timedelta(seconds=duration(rest[0])) if rest else None
            await self.bot.ban_chat_member(chat_id, target, until_date=until, revoke_messages=False)
            text = f"Member {target} banned" + (f" for {rest[0]}." if rest else ".")
        elif name == "unban":
            await self.bot.unban_chat_member(chat_id, target, only_if_banned=True)
            text = f"Member {target} unbanned. They can rejoin using an invite."
        elif name == "kick":
            await self.bot.ban_chat_member(chat_id, target, revoke_messages=False)
            await self.bot.unban_chat_member(chat_id, target, only_if_banned=True)
            text = f"Member {target} removed. They can rejoin using an invite."
        elif name == "mute":
            if not rest:
                raise UserError("Use /mute USER DURATION, for example /mute 123456 30m.")
            await self.mod.mute(chat_id, target, duration(rest[0]))
            text = f"Member {target} muted for {rest[0]}."
        elif name == "unmute":
            await self.mod.unmute(chat_id, target)
            text = f"Mute removed for member {target}. Group permissions still apply."
        elif name == "warn":
            if len(" ".join(rest)) < 3:
                raise UserError("Use /warn USER REASON.")
            text = await self.mod.warn(chat_id, target, uid, " ".join(rest))
        elif name == "unwarn":
            await self.db.run("UPDATE warnings SET active=0 WHERE id=(SELECT MAX(id) FROM warnings WHERE chat_id=? AND user_id=? AND active=1)", (chat_id, target))
            text = f"Most recent active warning removed for member {target}."
        else:
            rows = await self.db.rows("SELECT reason FROM warnings WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 10", (chat_id, target))
            count = await self.db.get("SELECT COUNT(*) n FROM warnings WHERE chat_id=? AND user_id=? AND active=1", (chat_id, target))
            text = f"Member {target} · {count['n']} active warnings\n" + "\n".join(r["reason"] for r in rows)
        await self.db.audit(uid, name, str(target), {"chat_id": chat_id})
        await self.ui.say(update, text)

    async def chat_action(self, name, update, args, chat_id):
        uid = update.effective_user.id
        right = {"del": "can_delete_messages", "purge": "can_delete_messages", "pin": "can_pin_messages" if chat_id == self.config.group_id else "can_edit_messages", "unpin": "can_pin_messages" if chat_id == self.config.group_id else "can_edit_messages", "lock": "can_restrict_members", "unlock": "can_restrict_members", "settitle": "can_change_info", "setdescription": "can_change_info", "invite": "can_invite_users", "revokeinvite": "can_invite_users", "promote": "can_promote_members", "demote": "can_promote_members"}.get(name)
        await self.access.require(chat_id, uid, right, owner_only=name in {"promote", "demote"})
        reply = update.effective_message.reply_to_message
        if name in {"del", "purge", "pin", "unpin"}:
            if not reply or reply.chat_id != chat_id:
                raise UserError(f"Reply to a message in the selected chat to use /{name}.")
            if name == "del":
                await self.bot.delete_message(chat_id, reply.message_id)
            elif name == "purge":
                end = update.effective_message.message_id
                if end - reply.message_id >= 100 or end < reply.message_id:
                    raise UserError("Reply to a message within the last 100 messages.")
                await self.bot.delete_messages(chat_id, list(range(reply.message_id, end + 1)))
            elif name == "pin":
                await self.bot.pin_chat_message(chat_id, reply.message_id, disable_notification=True)
            else:
                await self.bot.unpin_chat_message(chat_id, reply.message_id)
        elif name in {"lock", "unlock"}:
            if chat_id != self.config.group_id:
                raise UserError("Group writing locks apply to the group. Select /admin group.")
            await self.mod.lock(chat_id, name == "unlock")
        elif name in {"settitle", "setdescription"}:
            text = " ".join(args)
            if not text or len(text) > (128 if name == "settitle" else 255):
                raise UserError("Include text within Telegram’s title or description limit.")
            if name == "settitle":
                await self.bot.set_chat_title(chat_id, text)
            else:
                await self.bot.set_chat_description(chat_id, text)
        elif name == "invite":
            invite = await self.bot.create_chat_invite_link(chat_id, expire_date=datetime.now(UTC) + timedelta(hours=1), member_limit=1)
            await self.db.set_kv(f"invites:{chat_id}", invite.invite_link, True)
            await self.ui.say(update, f"One-use invite · expires in 1 hour\n{invite.invite_link}")
            await self.db.audit(uid, name, str(chat_id))
            return
        elif name == "revokeinvite":
            if not args or not await self.db.kv(f"invites:{chat_id}", args[0], False):
                raise UserError("Include an invite link created by this bot for this chat.")
            await self.bot.revoke_chat_invite_link(chat_id, args[0])
        elif name == "admins":
            admins = await self.bot.get_chat_administrators(chat_id)
            await self.ui.say(update, "Chat administrators\n" + "\n".join(f"{m.user.full_name} · {m.user.id} · {m.status}" for m in admins))
            return
        elif name in {"promote", "demote"}:
            if update.effective_chat.type != "private":
                raise UserError("Manage staff roles in private chat using /admin group or /admin channel.")
            target, _ = await self.target(update, args)
            if target in self.config.owners or target == self.bot.id:
                raise UserError("That account is protected.")
            member = await self.bot.get_chat_member(chat_id, target)
            if name == "promote":
                if member.status != "member":
                    raise UserError("Only a regular member can receive the limited moderator role.")
                await self.bot.promote_chat_member(chat_id, target, can_manage_chat=True, can_delete_messages=True,
                                                  can_restrict_members=True, can_pin_messages=chat_id == self.config.group_id,
                                                  can_edit_messages=chat_id == self.config.channel_id,
                                                  can_post_messages=chat_id == self.config.channel_id,
                                                  can_promote_members=False, can_invite_users=False,
                                                  can_change_info=False, can_manage_video_chats=False, is_anonymous=False)
                await self.db.set_kv(f"staff:{chat_id}", str(target), True)
            else:
                if not await self.db.kv(f"staff:{chat_id}", str(target), False):
                    raise UserError("This bot only demotes administrators it promoted. Manage other roles in Telegram.")
                await self.bot.promote_chat_member(chat_id, target, is_anonymous=False, can_manage_chat=False,
                                                  can_change_info=False, can_post_messages=False, can_edit_messages=False,
                                                  can_delete_messages=False, can_invite_users=False, can_restrict_members=False,
                                                  can_pin_messages=False, can_promote_members=False, can_manage_video_chats=False,
                                                  can_manage_topics=False, can_post_stories=False, can_edit_stories=False,
                                                  can_delete_stories=False, can_manage_direct_messages=False)
                await self.db.run("DELETE FROM kv WHERE scope=? AND key=?", (f"staff:{chat_id}", str(target)))
        await self.db.audit(uid, name, str(chat_id))
        await self.ui.say(update, "Action completed.")

    async def settings_command(self, name, update, args, chat_id):
        uid = update.effective_user.id
        settings = await self.s.settings(chat_id)
        if name == "settings":
            rows = [[(f"{key}: {'on' if settings[key] else 'off'}", f"toggle:{key}")] for key in ("antiflood", "antilink", "block_media", "block_sender_chats", "captcha")]
            text = ("Group protections\nToggle a setting below.\n\n"
                    f"Flood limit: {settings['flood_count']} messages/{settings['flood_seconds']} seconds\n"
                    f"Warning limit: {settings['warn_limit']} · Mute: {settings['mute_minutes']} minutes\n"
                    f"Quiet hours: {settings['quiet_start']}–{settings['quiet_end']} (-1 disables)\n\n"
                    "Use /set KEY VALUE. Keys: rules, welcome, goodbye, warn_limit, flood_count, flood_seconds, mute_minutes, quiet_start, quiet_end. Times use Australia/Perth by default.")
            await self.ui.say(update, text, buttons(rows))
        elif name == "set":
            if len(args) < 2:
                raise UserError("Use /set KEY VALUE. Use /settings to see keys.")
            key, value = args[0], " ".join(args[1:])
            bounds = {"warn_limit": (1, 20), "flood_count": (2, 50), "flood_seconds": (2, 60), "mute_minutes": (1, 10080), "quiet_start": (-1, 23), "quiet_end": (-1, 23)}
            if key in bounds:
                try:
                    value = int(value)
                except ValueError as exc:
                    raise UserError("This setting needs a whole number.") from exc
                if not bounds[key][0] <= value <= bounds[key][1]:
                    raise UserError(f"Allowed range: {bounds[key][0]} to {bounds[key][1]}.")
            elif key in {"rules", "welcome", "goodbye"}:
                if len(value) > (2500 if key == "rules" else 700):
                    raise UserError("Please use a shorter message.")
                value = "" if value == "off" else value
            else:
                raise UserError("Unknown setting. Use /settings to see supported keys.")
            await self.s.set_setting(chat_id, key, value, uid)
            await self.ui.say(update, f"{key} updated.")
        elif name == "filters":
            await self.ui.say(update, "Blocked phrases\n" + ("\n".join(settings["banned_words"]) or "None."))
        else:
            if len(args) < 2 or args[0] not in {"add", "remove"}:
                raise UserError("Use /filter add PHRASE or /filter remove PHRASE.")
            phrase = " ".join(args[1:]).casefold()
            if len(phrase) > 80 or len(phrase) < 2:
                raise UserError("Use a phrase between 2 and 80 characters.")
            words = set(settings["banned_words"])
            words.add(phrase) if args[0] == "add" else words.discard(phrase)
            if len(words) > 100:
                raise UserError("The blocked phrase limit is 100. Remove one first.")
            await self.s.set_setting(chat_id, "banned_words", sorted(words), uid)
            await self.ui.say(update, "Blocked phrases updated.")

    async def review_command(self, name, update, args):
        if name == "queue":
            rows = await self.db.rows("SELECT id,kind,status FROM entries WHERE status='pending' OR (kind='report' AND status='published' AND reviewed_at IS NULL) ORDER BY id LIMIT 20")
            await self.ui.say(update, "Review queue\n" + ("\n".join(f"#{r['id']} · {r['kind']} · {r['status']}" for r in rows) or "No submissions awaiting review.") + "\n\nUse /review ID. Reports configured for immediate delivery are already public.")
            return
        entry_id = self.number(args, f"/{name} ID" + (" REASON" if name in {"reject", "resolve"} else ""))
        if name == "review":
            row = await self.db.get("SELECT * FROM entries WHERE id=?", (entry_id,))
            if not row:
                raise UserError("Submission not found.")
            subject = "@" + row["subject_handle"] if row["subject_handle"] else str(row["target_id"] or "—")
            text = f"Submission #{entry_id} · {row['kind']} · {row['status']}\nSubject: {subject}\n\n{row['body']}\n\nUse /approve, /reject ID REASON or /resolve ID REASON."
            await self.ui.say(update, text, buttons([[("Approve / mark reviewed", f"review:{entry_id}" )]]))
            return
        row = await self.s.review(entry_id, update.effective_user.id, name, " ".join(args[1:])[:1000])
        if name in {"reject", "resolve"}:
            failures = await self.ui.close_public(row, row["status"])
        else:
            failures = 0
        await self.ui.say(update, f"Submission #{entry_id}: {row['status']}." + (" Public copies need a manual check; see /deliveries." if failures else ""))

    async def owner_command(self, name, update, args, chat_id):
        uid = update.effective_user.id
        if name in {"schedule", "repeat"}:
            if len(args) < 3 or args[0] not in {"group", "channel"}:
                raise UserError(f"Use /{name} group|channel DURATION TEXT.")
            destination = self.config.group_id if args[0] == "group" else self.config.channel_id
            await self.access.require(destination, uid, "can_post_messages" if args[0] == "channel" else None)
            seconds = duration(args[1])
            text = " ".join(args[2:])
            if len(text) > 2000:
                raise UserError("Scheduled text must be at most 2000 characters.")
            count = await self.db.get("SELECT COUNT(*) n FROM timers WHERE active=1")
            if count["n"] >= 50:
                raise UserError("There are already 50 active timers. Cancel one first.")
            timer_id = await self.db.run("INSERT OR IGNORE INTO timers(chat_id,body,next_run,interval_seconds,author_id,source_key) VALUES(?,?,?,?,?,?)",
                                         (destination, text, now() + seconds, seconds if name == "repeat" else 0, uid, f"update:{update.update_id}"))
            await self.db.audit(uid, name, str(timer_id), {"chat_id": destination})
            await self.ui.say(update, f"Timer #{timer_id} saved. First delivery in {args[1]}.")
        elif name == "timers":
            rows = await self.db.rows("SELECT * FROM timers WHERE active=1 ORDER BY next_run LIMIT 50")
            await self.ui.say(update, "Active timers\n" + ("\n".join(f"#{r['id']} · {r['chat_id']} · in {max(0, r['next_run'] - now())}s · repeat {r['interval_seconds']}s" for r in rows) or "None."))
        elif name == "canceltimer":
            timer_id = self.number(args, "/canceltimer ID")
            async with self.db.tx() as conn:
                await conn.execute("UPDATE timers SET active=0 WHERE id=?", (timer_id,))
                await conn.execute("UPDATE deliveries SET status='canceled' WHERE dedupe_key LIKE ? AND status='queued'", (f"timer:{timer_id}:%",))
                await audit(conn, uid, "cancel_timer", str(timer_id))
            await self.ui.say(update, f"Timer #{timer_id} canceled. Already sent messages are retained.")
        elif name in {"setcommand", "delcommand", "autoreply", "delreply"}:
            await self.custom_command(name, update, args)
        elif name == "deliveries":
            page = max(0, int(args[0]) - 1) if args and args[0].isdigit() else 0
            rows = await self.db.rows("SELECT * FROM deliveries ORDER BY id DESC LIMIT 10 OFFSET ?", (page * 10,))
            await self.ui.say(update, "Delivery history\n" + ("\n\n".join(f"#{r['id']} · {r['status']} · chat {r['chat_id']} · entry {r['entry_id'] or 'timer'}\n{r['note']}" for r in rows) or "None.") + f"\n\nMore: /deliveries {page + 2}\n/retry ID opens a delivery for review.")
        elif name == "retry":
            delivery_id = self.number(args, "/retry DELIVERY_ID")
            row = await self.db.get("SELECT * FROM deliveries WHERE id=? AND status IN ('failed','uncertain')", (delivery_id,))
            if not row:
                raise UserError("Choose a failed or uncertain delivery from /deliveries.")
            if row["entry_id"]:
                entry = await self.db.get("SELECT status FROM entries WHERE id=?", (row["entry_id"],))
                if entry["status"] in {"withdrawn", "rejected", "resolved"}:
                    raise UserError("That submission is closed and cannot be republished.")
            if len(args) < 2 or args[1] != "confirm":
                await self.ui.say(update, f"Retry delivery #{delivery_id}\nDestination: {row['chat_id']}\nStatus: {row['status']}\n\n{row['body']}\n\nCheck the destination first: an uncertain delivery may already be visible. To send again: /retry {delivery_id} confirm")
                return
            await self.db.run("UPDATE deliveries SET status='queued',next_attempt=?,note='Owner requested retry.' WHERE id=? AND status IN ('failed','uncertain')", (now(), delivery_id))
            await self.db.audit(uid, "retry_delivery", str(delivery_id))
            await self.ui.say(update, f"Delivery #{delivery_id} queued again.")
        elif name == "canceldelivery":
            delivery_id = self.number(args, "/canceldelivery ID")
            await self.db.run("UPDATE deliveries SET status='canceled',note='Canceled by owner.' WHERE id=? AND status IN ('queued','failed','uncertain')", (delivery_id,))
            await self.db.audit(uid, name, str(delivery_id))
            await self.ui.say(update, "Eligible queued or held delivery canceled. Check the destination for uncertain sends.")
        elif name == "audit":
            page = max(0, int(args[0]) - 1) if args and args[0].isdigit() else 0
            rows = await self.db.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 15 OFFSET ?", (page * 15,))
            await self.ui.say(update, "Audit log\n" + "\n".join(f"#{r['id']} · {r['action']} · actor {r['actor_id']} · target {r['target']}" for r in rows) + f"\n\nMore: /audit {page + 2}")
        elif name == "inbox":
            rows = await self.db.rows("SELECT update_id,note FROM inbox WHERE status='held' ORDER BY update_id DESC LIMIT 20")
            await self.ui.say(update, "Interrupted or failed incoming actions\n" + ("\n".join(f"Update {r['update_id']} · {r['note']}" for r in rows) or "None.") + "\n\nInspect saved state before repeating an interrupted moderation action. Message bodies are not retained after processing.")
        elif name == "health":
            text = "Circuit Perth · Health\nDatabase: connected\n"
            for label, destination in (("Group", self.config.group_id), ("Channel", self.config.channel_id)):
                try:
                    member = await self.bot.get_chat_member(destination, self.bot.id)
                    text += f"{label}: {member.status}\n"
                except TelegramError:
                    text += f"{label}: unavailable; check chat ID and permissions\n"
            rows = await self.db.rows("SELECT status,COUNT(*) n FROM deliveries GROUP BY status")
            text += "\n" + "\n".join(f"Deliveries {r['status']}: {r['n']}" for r in rows)
            await self.ui.say(update, text)

    async def custom_command(self, name, update, args):
        kind = "commands" if name in {"setcommand", "delcommand"} else "replies"
        values = await self.db.kv("custom", kind, {})
        if name == "setcommand":
            if len(args) < 2 or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", args[0]) or args[0] in RESERVED:
                raise UserError("Use /setcommand NAME RESPONSE with an unused lowercase command name.")
            key, value = args[0], " ".join(args[1:])
        elif name == "autoreply":
            parts = " ".join(args).split("|", 1)
            if len(parts) != 2 or not 2 <= len(parts[0].strip()) <= 80:
                raise UserError("Use /autoreply KEYWORD | RESPONSE.")
            key, value = parts[0].strip().casefold(), parts[1].strip()
        else:
            key = " ".join(args).strip().casefold()
            values.pop(key, None)
            await self.db.set_kv("custom", kind, values)
            await self.ui.say(update, "Custom response removed.")
            return
        if not 1 <= len(value) <= 1500:
            raise UserError("Response must be 1–1500 characters.")
        if key not in values and len(values) >= 50:
            raise UserError("The custom response limit is 50. Remove one first.")
        values[key] = value
        await self.db.set_kv("custom", kind, values)
        await self.db.audit(update.effective_user.id, name, key)
        await self.ui.say(update, "Custom response saved.")

    async def callback(self, update) -> bool:
        query = update.callback_query
        parts = (query.data or "").split(":")
        if parts[0] == "toggle" and len(parts) == 2:
            uid = query.from_user.id
            await self.access.require(self.config.group_id, uid, "can_change_info")
            key = parts[1]
            if key not in {"antiflood", "antilink", "block_media", "block_sender_chats", "captcha"}:
                raise UserError("Unknown setting.")
            settings = await self.s.settings(self.config.group_id)
            if key == "captcha" and not settings[key]:
                await self.access.require(self.config.group_id, uid, "can_restrict_members")
            if key == "antiflood":
                await self.access.require(self.config.group_id, uid, "can_restrict_members")
            if key in {"antiflood", "antilink", "block_media", "block_sender_chats"}:
                await self.access.require(self.config.group_id, uid, "can_delete_messages")
            await self.s.set_setting(self.config.group_id, key, not settings[key], uid)
            await self.settings_command("settings", update, [], self.config.group_id)
        elif parts[0] == "review" and len(parts) == 2 and parts[1].isdigit():
            if update.effective_chat.type != "private":
                raise UserError("Review submissions in private chat.")
            await self.access.reviewer(query.from_user.id)
            await self.review_command("approve", update, [parts[1]])
        else:
            return False
        return True
