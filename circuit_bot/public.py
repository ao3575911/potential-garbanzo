from __future__ import annotations

import re

from .privacy import redact
from .service import UserError
from .ui import UI, buttons

HELP = """Circuit Perth Bot

/vouch — vouch for a member
/report — submit a scam report
/check @username — member dossier
/self — your dossier in private
/post — anonymous chat to the channel or group
/mysubmissions — your submissions and delivery status
/withdraw ID — withdraw your submission
/appeal ID — appeal a report about you
/handle @username — unverified handle-only records
/rules — community rules
/privacy — what the bot stores
/id — your user ID and this chat ID
/cancel — close your current form
/help — this command list

Administrators: /admin for moderation and settings."""

PRIVACY = """Privacy

Posts and reports publish without your sender profile or forwarding headers. Detectable usernames, links, email addresses, phone numbers and your observed name are removed from the message body. Free-text details can still identify you; leave those out. Anonymous chat is text only.

The self-hosting owner can access Telegram IDs, current observed profiles, submissions, moderation history and delivery records in the database. Public dossiers omit contributor identities. The bot sees only data Telegram delivers to it; it cannot see past chat history, account creation dates or private conversations elsewhere.

Use /mysubmissions and /withdraw ID to manage your submissions. Contact a community administrator for data access or correction. Telegram and recipients may retain published messages."""


class Public:
    def __init__(self, ui: UI, access):
        self.ui, self.s, self.db, self.config, self.access = ui, ui.s, ui.db, ui.config, access

    async def command(self, name: str, update, args: list[str]) -> bool:
        user = update.effective_user
        if name == "id":
            await self.ui.say(update, f"Your Telegram ID: {user.id}\nChat ID: {update.effective_chat.id}\nChat type: {update.effective_chat.type}")
        elif name in {"help", "start"}:
            if name == "start" and args and await self.deep_link(update, args[0]):
                return True
            await self.ui.say(update, HELP)
        elif name == "privacy":
            if await self.ui.private(update):
                await self.ui.say(update, PRIVACY)
        elif name == "rules":
            settings = await self.s.settings(self.config.group_id)
            await self.ui.say(update, "Circuit Perth · Rules\n\n" + settings["rules"])
        elif name == "cancel":
            if await self.ui.private(update):
                await self.s.cancel(user.id)
                await self.ui.say(update, "Form closed. Use /help to choose a command.")
        elif name in {"vouch", "report", "check"}:
            await self.start_feedback(name, update, args)
        elif name == "self":
            await self.ui.show_dossier(update, {"target_id": user.id, "subject_handle": user.username}, dm=True)
        elif name == "handle":
            if not args or not re.fullmatch(r"@?[a-z][a-z0-9_]{3,31}", args[0], re.I):
                raise UserError("Use /handle @username.")
            await self.ui.show_dossier(update, {"target_id": None, "subject_handle": args[0].lstrip("@").lower()})
        elif name == "post":
            if await self.ui.private(update, "post"):
                nonce = await self.s.begin(user.id, "post", "destination")
                await self.ui.say(update, "Anonymous chat\nWhere should your message go?", buttons([[("Bot2Channel", f"dest:{nonce}:channel"), ("Bot2Group", f"dest:{nonce}:group")]]))
        elif name in {"mysubmissions", "status"}:
            if await self.ui.private(update):
                await self.my_submissions(update, args, name == "status")
        elif name == "withdraw":
            if await self.ui.private(update):
                entry_id = self.entry_id(args, "/withdraw ID")
                entry = await self.s.withdraw(entry_id, user.id)
                failures = await self.ui.close_public(entry, "withdrawn")
                text = f"Submission #{entry_id} withdrawn."
                if failures:
                    text += " Some public copies could not be removed or confirmed. Please ask an administrator to check them."
                await self.ui.say(update, text)
        elif name == "appeal":
            if await self.ui.private(update):
                entry_id = self.entry_id(args, "/appeal REPORT_ID")
                entry = await self.db.get("SELECT * FROM entries WHERE id=? AND kind='report' AND target_id=? AND status IN ('published','resolved')", (entry_id, user.id))
                if not entry:
                    raise UserError("Choose a public report about your own Telegram user ID. For unverified handles, contact an administrator.")
                await self.s.begin(user.id, "appeal", "body", {"related_id": entry_id})
                await self.ui.say(update, f"Appeal report #{entry_id}\nSend your response in one text message (10–1200 characters). It will be sent privately to moderators.")
        else:
            return False
        return True

    @staticmethod
    def entry_id(args: list[str], usage: str) -> int:
        if not args or not args[0].isdigit() or int(args[0]) <= 0:
            raise UserError("Use " + usage + ".")
        return int(args[0])

    async def deep_link(self, update, payload: str) -> bool:
        if payload in {"post", "self"}:
            await self.command(payload, update, [])
            return True
        match = re.fullmatch(r"(vouch|report|check)_(\d+|h_[a-z0-9_]{4,32})", payload)
        if match:
            target = "@" + match[2][2:] if match[2].startswith("h_") else match[2]
            await self.start_feedback(match[1], update, [target])
            return True
        return False

    async def start_feedback(self, kind: str, update, args: list[str]):
        subject = None
        reply = update.effective_message.reply_to_message
        if args or (reply and reply.from_user and not reply.sender_chat and not reply.from_user.is_bot):
            subject = await self.ui.subject(args[0] if args else "", reply if not args else None)
        if update.effective_chat.type != "private":
            if kind == "check" and subject:
                await self.ui.show_dossier(update, subject)
                return
            key = str(subject["target_id"]) if subject and subject["target_id"] else "h_" + subject["subject_handle"] if subject else ""
            if not await self.ui.private(update, f"{kind}_{key}" if key else "start"):
                return
        if kind == "check" and subject:
            await self.ui.show_dossier(update, subject)
            return
        if subject and kind in {"vouch", "report"}:
            self.prevent_self(update, subject)
        await self.s.begin(update.effective_user.id, kind, "body" if subject else "target", subject)
        if subject:
            await self.body_prompt(update, kind, subject)
        elif kind == "report":
            await self.ui.say(update, "Scam report · 1 of 2\nWhat was the member’s @username?\n\nThe report will publish anonymously after your next answer. /cancel to stop.")
        else:
            await self.ui.say(update, f"{'Vouch' if kind == 'vouch' else 'Member dossier'}\nSend the member’s @username or known Telegram user ID. /cancel to stop.")

    def prevent_self(self, update, subject: dict):
        if subject.get("target_id") == update.effective_user.id or (
            update.effective_user.username and (subject.get("subject_handle") or "").lower() == update.effective_user.username.lower()
        ):
            raise UserError("Choose another member. You cannot vouch for or report yourself.")

    async def body_prompt(self, update, kind: str, subject: dict):
        label = "@" + subject["subject_handle"] if subject.get("subject_handle") else str(subject["target_id"])
        if kind == "report":
            text = f"Scam report · 2 of 2\nMember: {label}\nWhat is your scam report? Send one text message (10–1200 characters).\n\nIt will publish immediately without your sender details. Omit private contact details. /cancel to stop."
        else:
            text = f"Vouch for {label}\nDescribe your experience in one text message (10–1200 characters). Your vouch will immediately publish anonymously to the channel and appear in their dossier. /cancel to stop."
        await self.ui.say(update, text)

    async def message(self, update):
        if update.effective_chat.type != "private":
            return
        session = await self.s.session(update.effective_user.id)
        if not session:
            await self.ui.say(update, "Choose /vouch, /report, /check or /post to begin. Use /help for all commands.")
            return
        message = update.effective_message
        if not message.text or message.photo or message.document:
            raise UserError("Please send a text message. Photos, files and other media are not accepted in this form.")
        payload = session["payload"]
        kind, step = session["kind"], session["step"]
        if step == "target":
            subject = await self.ui.subject(message.text)
            if kind == "check":
                await self.s.cancel(update.effective_user.id)
                await self.ui.show_dossier(update, subject)
                return
            self.prevent_self(update, subject)
            await self.s.advance(update.effective_user.id, "body", subject)
            await self.body_prompt(update, kind, subject)
        elif step == "body":
            user = update.effective_user
            if not 10 <= len(message.text) <= 1200:
                raise UserError("Please send 10–1200 characters in one text message. Your form is still open.")
            body = redact(message.text, (user.full_name, user.first_name, user.last_name or "", user.username or ""), message.entities)
            if len(body.replace("[removed]", "").strip()) < 10:
                raise UserError("Too little text remains after removing identifiers. Please rewrite the message without contact details.")
            payload["body"] = body
            await self.s.advance(user.id, "preview", payload)
            await self.finish(update, session["nonce"])
        elif step == "preview":
            raise UserError("A submission is ready. Use the Retry button, or /cancel to discard it.")
        elif step == "destination":
            raise UserError("Choose Bot2Channel or Bot2Group using the buttons above, or use /post to start again.")

    async def finish(self, update, nonce: str):
        try:
            entry, created = await self.s.submit(update.effective_user.id, nonce)
        except UserError as exc:
            await self.ui.say(update, str(exc), buttons([[("Retry submission", f"submit:{nonce}")]]))
            return
        if entry["status"] == "pending":
            text = f"Submission #{entry['id']} received for moderator review."
        elif entry["kind"] == "vouch":
            text = f"Vouch #{entry['id']} added to the dossier and accepted for immediate channel delivery."
        elif entry["kind"] in {"report", "post"}:
            text = f"{'Scam report' if entry['kind'] == 'report' else 'Anonymous chat'} #{entry['id']} accepted for immediate delivery."
        else:
            text = f"Vouch #{entry['id']} added to the dossier."
        text += "\nUse /mysubmissions to check status."
        await self.ui.say(update, text)

    async def callback(self, update) -> bool:
        query = update.callback_query
        data = (query.data or "").split(":")
        if data[0] == "dest" and len(data) == 3:
            if update.effective_chat.type != "private":
                raise UserError("Use /post in private chat.")
            session = await self.s.session(query.from_user.id)
            if not session or session["nonce"] != data[1] or session["kind"] != "post" or session["step"] != "destination":
                raise UserError("This choice has expired or was replaced. Use /post again.")
            if data[2] not in {"group", "channel"}:
                raise UserError("Choose Bot2Channel or Bot2Group.")
            destination = self.config.group_id if data[2] == "group" else self.config.channel_id
            await self.s.advance(query.from_user.id, "body", {"destination": destination})
            await query.edit_message_text(f"Anonymous chat · {'Bot2Group' if data[2] == 'group' else 'Bot2Channel'}\n\nSend your message (10–1200 characters). Your sender profile and forwarding headers are not attached. Detectable usernames, links and contact details are removed. Omit any other identifying details.\n\nYour message will send immediately. /cancel to stop.")
        elif data[0] == "submit" and len(data) == 2:
            if update.effective_chat.type != "private":
                raise UserError("Submit from your private chat with the bot.")
            await self.finish(update, data[1])
        elif data[0] == "dos" and len(data) == 4:
            if str(query.from_user.id) != data[1]:
                raise UserError("Run /check to open your own dossier controls.")
            key = data[2]
            if not data[3].isdigit() or not (key.isdigit() or re.fullmatch(r"h_[a-z0-9_]{4,32}", key)):
                raise UserError("Invalid dossier link. Run /check again.")
            subject = {"target_id": int(key)} if key.isdigit() else {"subject_handle": key[2:]}
            await self.ui.show_dossier(update, subject, int(data[3]))
        else:
            return False
        return True

    async def my_submissions(self, update, args: list[str], single: bool):
        page = int(args[0]) - 1 if args and args[0].isdigit() else 0
        if single:
            entry_id = self.entry_id(args, "/status ID")
            rows = await self.db.rows("SELECT * FROM entries WHERE id=? AND author_id=?", (entry_id, update.effective_user.id))
        else:
            rows = await self.db.rows("SELECT * FROM entries WHERE author_id=? ORDER BY id DESC LIMIT 10 OFFSET ?", (update.effective_user.id, max(0, page) * 10))
        text = "Your submissions"
        for row in rows:
            deliveries = await self.db.rows("SELECT id,status FROM deliveries WHERE entry_id=?", (row["id"],))
            status = ", ".join(f"delivery {d['id']}: {d['status']}" for d in deliveries)
            text += f"\n\n#{row['id']} · {row['kind']} · {row['status']}" + (f"\n{status}" if status else "")
            if single:
                text += "\n" + row["body"]
        if not rows:
            text += "\nNo submissions found."
        if not single and len(rows) == 10:
            text += f"\n\nMore: /mysubmissions {page + 2}"
        await self.ui.say(update, text)
