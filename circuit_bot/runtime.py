from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import time
from collections import defaultdict, deque

from telegram import BotCommand, BotCommandScopeAllPrivateChats, Update
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import AIORateLimiter, Application, ContextTypes, TypeHandler

from .access import Access
from .admin import Admin
from .config import Config
from .db import Database, one
from .delivery import DeliveryWorker
from .moderation import Moderation
from .public import Public
from .service import Service, UserError, now
from .ui import UI

log = logging.getLogger(__name__)
ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query", "chat_member", "my_chat_member", "chat_join_request"]


class SecretFormatter(logging.Formatter):
    def format(self, record):
        return re.sub(r"\b\d+:[A-Za-z0-9_-]{25,}", "[BOT_TOKEN]", super().format(record))


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SecretFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


class Controller:
    def __init__(self, service: Service, bot):
        self.s, self.db, self.config, self.bot = service, service.db, service.config, bot
        self.access = Access(self.config, bot)
        self.ui = UI(service, bot)
        self.mod = Moderation(service, self.access, bot)
        self.public = Public(self.ui, self.access)
        self.admin = Admin(self.ui, self.access, self.mod)
        self.interactions: dict[int, float] = {}
        self.budgets: dict[int, deque] = defaultdict(deque)

    async def dispatch(self, update: Update, context=None):
        chat = update.effective_chat
        if not chat or (chat.type != "private" and chat.id not in {self.config.group_id, self.config.channel_id}):
            # /id is available before adding a chat to configuration.
            if update.effective_message and update.effective_message.text and update.effective_user and update.effective_message.text.split()[0].split("@")[0] == "/id":
                await self.public.command("id", update, [])
            return
        if update.my_chat_member:
            await self.db.audit(None, "bot_membership", str(chat.id), {"status": update.my_chat_member.new_chat_member.status})
            return
        if update.chat_join_request:
            request = update.chat_join_request
            await self.db.observe(request.from_user)
            await self.db.run("INSERT OR REPLACE INTO join_requests VALUES(?,?,?)", (chat.id, request.from_user.id, now()))
            return
        if update.chat_member:
            member = update.chat_member
            old = member.old_chat_member.status
            new = member.new_chat_member.status
            if old in {"left", "kicked"} and new in {"member", "restricted"}:
                await self.mod.joined(chat.id, member.new_chat_member.user)
            elif new in {"left", "kicked"}:
                settings = await self.s.settings(chat.id)
                if settings["goodbye"] and chat.id == self.config.group_id:
                    await self.bot.send_message(chat.id, settings["goodbye"].replace("{name}", member.new_chat_member.user.first_name), parse_mode=None)
            return
        user = update.effective_user
        message = update.effective_message
        if message and message.sender_chat and chat.id == self.config.group_id:
            trusted = message.sender_chat.id == chat.id or (message.sender_chat.id == self.config.channel_id and message.is_automatic_forward)
            if not trusted and (await self.s.settings(chat.id))["block_sender_chats"]:
                await self.access.require(chat.id, self.bot.id, "can_delete_messages")
                await self.bot.delete_message(chat.id, message.message_id)
                await self.db.audit(self.bot.id, "masked_sender_delete", str(message.sender_chat.id), {"chat_id": chat.id})
            return
        if not user or user.is_bot or (update.effective_message and update.effective_message.sender_chat):
            return  # Anonymous administrators act privately, where identity can be checked.
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except BadRequest:
                pass  # Expired callback acknowledgement does not discard a durable submission.
            if self.throttled(user.id):
                return
            await self.db.observe(user, dm=chat.type == "private")
            if (update.callback_query.data or "").startswith("cap:"):
                await self.mod.captcha_answer(update.callback_query, update.callback_query.data.split(":"))
            elif not await self.public.callback(update) and not await self.admin.callback(update):
                raise UserError("This button has expired. Use /help to choose a command.")
            return
        message = update.effective_message
        if not message:
            return
        is_message = bool(update.message or update.channel_post)
        # Only count real group messages, not edits, commands, or service updates.
        await self.db.observe(user, chat.id if chat.id == self.config.group_id else None,
                              dm=chat.type == "private", message=is_message and not message.new_chat_members and not message.left_chat_member)
        if message.new_chat_members or message.left_chat_member:
            return  # Member events are processed through chat_member updates without duplication.
        if chat.id == self.config.group_id and await self.mod.inspect(message):
            return
        if update.edited_message or update.edited_channel_post:
            return  # Edits are moderated, but never resubmit forms or repeat commands.
        text = message.text or ""
        if text.startswith("/"):
            token, *args = text.split()
            command, _, suffix = token[1:].partition("@")
            if suffix and suffix.lower() != self.bot.username.lower():
                return
            if self.throttled(user.id):
                return
            if await self.public.command(command.lower(), update, args):
                return
            if await self.admin.command(command.lower(), update, args):
                return
            custom = await self.db.kv("custom", "commands", {})
            if command.lower() in custom:
                await self.ui.say(update, custom[command.lower()])
            else:
                await self.ui.say(update, "Unknown command. Use /help for the command list.")
        elif chat.type == "private":
            if not self.throttled(user.id):
                await self.public.message(update)
        else:
            replies = await self.db.kv("custom", "replies", {})
            for keyword, reply in replies.items():
                if keyword in text.casefold() and not self.throttled(user.id, seconds=5):
                    await self.ui.say(update, reply)
                    break

    def throttled(self, user_id: int, seconds: float = 0) -> bool:
        current = time.monotonic()
        if seconds:
            if current - self.interactions.get(user_id, 0) < seconds:
                return True
            self.interactions[user_id] = current
            return False
        events = self.budgets[user_id]
        while events and events[0] <= current - 10:
            events.popleft()
        if len(events) >= 20:
            return True
        events.append(current)
        return False

    async def errors(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        exc = context.error
        update_id = update.update_id if isinstance(update, Update) else None
        if not isinstance(exc, UserError) and update_id is not None:
            await self.db.run("UPDATE inbox SET note=? WHERE update_id=?", (type(exc).__name__, update_id))
        log.warning("handler_error update=%s type=%s", update_id, type(exc).__name__)
        if isinstance(update, Update) and update.effective_chat:
            if isinstance(exc, UserError):
                message = str(exc)
            elif isinstance(exc, Forbidden):
                message = "Telegram denied this action. Check the bot’s chat permissions, or open a private chat with the bot."
            elif isinstance(exc, BadRequest):
                message = "Telegram could not apply this action. Check the target, message age and bot permissions."
            elif isinstance(exc, RetryAfter):
                message = "Telegram is limiting requests. Please wait a moment, then try again."
            elif isinstance(exc, NetworkError):
                message = "Telegram’s response was interrupted. Check /mysubmissions before submitting again. Administrators can check /deliveries."
            else:
                message = "I could not complete that action. Your saved submissions remain available. Use /mysubmissions, or try the command again."
            try:
                await self.ui.say(update, message)
            except TelegramError:
                pass


class Intake:
    """Persist each Telegram batch before advancing the long-polling offset."""

    def __init__(self, db: Database, bot):
        self.db, self.bot = db, bot

    async def recover(self):
        await self.db.run("UPDATE inbox SET status='held',raw='',note='Restart during processing; inspect bot state before repeating the command.' WHERE status='processing'")

    async def save_batch(self, updates: list[Update]):
        if not updates:
            return
        async with self.db.tx() as conn:
            for update in updates:
                await conn.execute("INSERT OR IGNORE INTO inbox VALUES(?,?,'queued',?,'')", (update.update_id, update.to_json(), now()))
            await conn.execute("INSERT OR REPLACE INTO kv VALUES('runtime','offset',?)", (json.dumps(updates[-1].update_id + 1),))

    async def poll(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                offset = await self.db.kv("runtime", "offset", 0)
                updates = await self.bot.get_updates(offset=offset, timeout=25, read_timeout=35, allowed_updates=ALLOWED_UPDATES)
                await self.save_batch(updates)
            except RetryAfter as exc:
                delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else exc.retry_after
                await wait_or_stop(stop, min(float(delay) + 1, 300))
            except (Conflict, InvalidToken):
                log.error("Polling stopped: invalid token, webhook enabled, or a second bot instance is running.")
                raise
            except NetworkError:
                log.warning("Telegram unavailable; reconnecting.")
                await wait_or_stop(stop, 3)

    async def consume(self, app: Application, stop: asyncio.Event):
        while not stop.is_set():
            async with self.db.tx() as conn:
                row = await one(conn, "SELECT * FROM inbox WHERE status='queued' ORDER BY update_id LIMIT 1")
                if row:
                    await conn.execute("UPDATE inbox SET status='processing' WHERE update_id=?", (row["update_id"],))
            if not row:
                await wait_or_stop(stop, 0.1)
                continue
            update = Update.de_json(json.loads(row["raw"]), self.bot)
            await app.process_update(update)
            await self.db.run("UPDATE inbox SET status=CASE WHEN note='' THEN 'done' ELSE 'held' END,raw='' WHERE update_id=?", (row["update_id"],))


async def wait_or_stop(stop: asyncio.Event, delay: float):
    try:
        await asyncio.wait_for(stop.wait(), delay)
    except TimeoutError:
        pass


async def maintenance(controller: Controller, worker: DeliveryWorker, stop: asyncio.Event):
    last_cleanup = 0
    while not stop.is_set():
        await controller.s.tick_timers()
        await controller.mod.expire_captchas()
        for _ in range(8):
            if not await worker.run_once():
                break
        controller.config.data_dir.joinpath("heartbeat").write_text(str(now()))
        if now() - last_cleanup > 60:
            controller.mod.prune()
            controller.interactions = {k: v for k, v in controller.interactions.items() if time.monotonic() - v < 120}
            controller.budgets = defaultdict(deque, {k: v for k, v in controller.budgets.items() if v and time.monotonic() - v[-1] < 120})
            await controller.db.run("DELETE FROM sessions WHERE expires_at<=?", (now(),))
            await controller.db.run("DELETE FROM inbox WHERE status='done' AND created_at<?", (now() - 604800,))
            last_cleanup = now()
        await wait_or_stop(stop, 1)


async def run(config: Config):
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The lock remains held for the complete process lifetime, including startup.
    import fcntl

    lock = config.data_dir.joinpath("bot.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise UserError("Another Circuit Perth process is already using this data directory.") from exc
    db = Database(config.db_path)
    tasks = []
    app = None
    try:
        await db.open()
        app = (Application.builder().token(config.token).updater(None).concurrent_updates(False)
               .rate_limiter(AIORateLimiter(max_retries=0)).connect_timeout(10).read_timeout(30)
               .write_timeout(30).pool_timeout(10).connection_pool_size(16).build())
        controller = Controller(Service(db, config), app.bot)
        app.add_handler(TypeHandler(Update, controller.dispatch))
        app.add_error_handler(controller.errors)
        await app.initialize()
        try:
            webhook = await app.bot.get_webhook_info()
            if webhook.url:
                raise UserError("This token has a webhook. Remove it deliberately with Telegram deleteWebhook before running polling. Pending updates are not dropped.")
            group, channel = await asyncio.gather(app.bot.get_chat(config.group_id), app.bot.get_chat(config.channel_id))
            if group.type != "supergroup" or channel.type != "channel":
                raise UserError("GROUP_ID must be a supergroup and CHANNEL_ID must be a channel.")
            await controller.access.require(config.group_id, next(iter(config.owners)), "can_delete_messages")
            await controller.access.require(config.group_id, next(iter(config.owners)), "can_restrict_members")
            await controller.access.require(config.channel_id, next(iter(config.owners)), "can_post_messages")
            # No drop_pending_updates and no automatic webhook deletion.
            worker, intake = DeliveryWorker(db, app.bot, config.owners), Intake(db, app.bot)
            await worker.recover()
            await intake.recover()
            await controller.db.run("UPDATE captchas SET status='held' WHERE status='expiring'")
            await app.bot.set_my_commands([BotCommand(k, v) for k, v in [
                ("vouch", "Vouch for a member"), ("report", "Submit a scam report"),
                ("check", "View a member dossier"), ("self", "View your dossier"),
                ("post", "Send anonymous chat"), ("mysubmissions", "Your submissions and status"),
                ("rules", "Community rules"), ("privacy", "Data and anonymity"),
                ("cancel", "Close your current form"), ("help", "All commands"),
                ("id", "Show user and chat IDs"), ("admin", "Administrator tools"),
            ]], scope=BotCommandScopeAllPrivateChats())
            await app.start()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            log.info("Circuit Perth Bot started; private data stays in the configured data directory.")
            tasks = [asyncio.create_task(intake.poll(stop)), asyncio.create_task(intake.consume(app, stop)),
                     asyncio.create_task(maintenance(controller, worker, stop)), asyncio.create_task(stop.wait())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            stop.set()
            for task in done:
                task.result()  # Surface fatal failures to the process supervisor.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.stop()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if app.running:
                await app.stop()
            await app.shutdown()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if app and app.running:
            await app.stop()
        await db.close()
        lock.close()
