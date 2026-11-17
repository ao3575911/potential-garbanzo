from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from .config import Config, ConfigError
from .db import Database
from .runtime import configure_logging, run
from .service import UserError


async def doctor(config: Config) -> bool:
    success = True
    async with Bot(config.token) as bot:
        print(f"Bot: @{bot.username}")
        webhook = await bot.get_webhook_info()
        print("Delivery: " + ("webhook configured; remove it before running polling" if webhook.url else "long polling available"))
        if webhook.url:
            success = False
        for name, chat_id, expected, rights in (
            ("Group", config.group_id, "supergroup", ["can_delete_messages", "can_restrict_members", "can_invite_users", "can_pin_messages", "can_change_info"]),
            ("Channel", config.channel_id, "channel", ["can_post_messages", "can_edit_messages", "can_delete_messages", "can_invite_users", "can_change_info"]),
        ):
            try:
                chat = await bot.get_chat(chat_id)
                member = await bot.get_chat_member(chat_id, bot.id)
                missing = [r.removeprefix("can_") for r in rights if not getattr(member, r, False)]
                good = chat.type == expected and member.status == "administrator" and not missing
                success &= good
                print(f"{name}: type={chat.type}; role={member.status}; " + ("ready" if good else f"check type and rights: {', '.join(missing)}"))
            except TelegramError:
                success = False
                print(f"{name}: unavailable; check the configured ID and add the bot as administrator.")
    print("Owners: " + ", ".join(str(v) for v in sorted(config.owners)))
    print(f"Report destination: {config.report_destination}; post review: {config.review_posts}; report review: {config.review_reports}")
    return bool(success)


async def discover(token: str):
    async with Bot(token) as bot:
        if (await bot.get_webhook_info()).url:
            raise UserError("This bot has a webhook. Remove it deliberately before discovery.")
        print(f"Listening as @{bot.username} for 2 minutes. Stop any other instance first.")
        print("Send /id to the bot privately. Add it to the supergroup and channel as admin, then send /id in each.")
        print("This reads updates for setup. Run it before production; do not use it alongside the running bot.")
        deadline, offset = time.monotonic() + 120, 0
        seen = set()
        while time.monotonic() < deadline:
            updates = await bot.get_updates(offset=offset, timeout=15, allowed_updates=["message", "channel_post", "my_chat_member"])
            for update in updates:
                offset = update.update_id + 1
                chat, user = update.effective_chat, update.effective_user
                if chat and chat.id not in seen:
                    print(f"Chat type={chat.type} ID={chat.id}")
                    seen.add(chat.id)
                if user and ("user", user.id) not in seen:
                    print(f"User ID={user.id}" + (" (use your own ID for OWNER_IDS)" if chat and chat.type == "private" else ""))
                    seen.add(("user", user.id))


def exclusive_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(text)


def setup(path: Path):
    if path.exists():
        raise UserError(f"{path.name} already exists. Edit it locally; setup will not overwrite it.")
    print("Circuit Perth setup. Secrets are stored only in your local .env file.")
    token = getpass.getpass("BotFather token (hidden): ").strip()
    if input("Discover user and chat IDs now? [y/N]: ").strip().lower() == "y":
        asyncio.run(discover(token))
    owners = input("Your numeric Telegram user ID (or comma-separated owner IDs): ").strip()
    group = input("Supergroup ID (negative number): ").strip()
    channel = input("Channel ID (negative number): ").strip()
    import re

    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{25,}", token):
        raise UserError("Token format is invalid. Nothing was written.")
    if not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", owners) or not re.fullmatch(r"-\d+", group) or not re.fullmatch(r"-\d+", channel) or group == channel:
        raise UserError("IDs are invalid. Nothing was written.")
    exclusive_write(path, f"BOT_TOKEN={token}\nOWNER_IDS={owners}\nGROUP_ID={group}\nCHANNEL_ID={channel}\nDATA_DIR=data\nTIMEZONE=Australia/Perth\nREPORT_DESTINATION=channel\nREVIEW_POSTS=false\nREVIEW_REPORTS=false\nREVIEW_VOUCHES=false\nDAILY_SUBMISSION_LIMIT=20\nSESSION_MINUTES=30\n")
    print(f"Saved {path.name} with private file permissions. Run: circuit-bot doctor")


async def backup(config: Config, destination: Path):
    if destination.exists():
        raise UserError("Backup destination already exists; choose a new filename.")
    if not config.db_path.exists():
        raise UserError("No bot database exists yet. Start the bot once before backing it up.")
    db = Database(config.db_path)
    try:
        await db.open()
        await db.backup(destination)
    finally:
        await db.close()
    print(f"Backup saved: {destination}")


def restore(config: Config, source: Path):
    import fcntl
    import shutil

    if not source.is_file() or source.resolve() == config.db_path.resolve():
        raise UserError("Choose a separate valid backup file.")
    with sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise UserError("Backup validation failed or schema version is unsupported.")
        required = {"users", "entries", "sessions", "deliveries", "inbox", "kv"}
        actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= actual:
            raise UserError("This file is not a complete Circuit Perth database.")
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with config.data_dir.joinpath("bot.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UserError("Stop the bot before restoring a database.") from exc
        if config.db_path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            previous = config.data_dir / f"before-restore-{stamp}.sqlite3"
            with sqlite3.connect(config.db_path) as live, sqlite3.connect(previous) as saved:
                live.backup(saved)
            previous.chmod(0o600)
        for suffix in ("-wal", "-shm"):
            Path(str(config.db_path) + suffix).unlink(missing_ok=True)
        temp = config.data_dir / "restore.tmp"
        shutil.copyfile(source, temp)
        temp.chmod(0o600)
        temp.replace(config.db_path)
    print("Database restored. Review /deliveries and /inbox after restarting. Do not run an old and restored copy together.")


async def export(config: Config, destination: Path, user_id: int | None):
    if user_id is not None and user_id <= 0:
        raise UserError("User ID must be a positive Telegram user ID.")
    if not config.db_path.exists():
        raise UserError("No bot database exists yet.")
    db = Database(config.db_path)
    try:
        await db.open()
        data = {}
        for table in ("users", "entries", "members", "warnings", "audit"):
            where, args = "", ()
            if user_id:
                where = {"users": " WHERE id=?", "entries": " WHERE author_id=? OR target_id=?", "members": " WHERE user_id=?", "warnings": " WHERE user_id=?", "audit": " WHERE actor_id=?"}[table]
                args = (user_id, user_id) if table == "entries" else (user_id,)
            data[table] = await db.rows(f"SELECT * FROM {table}{where}", args)
        exclusive_write(destination, json.dumps(data, ensure_ascii=False, indent=2))
    finally:
        await db.close()
    print(f"Sensitive export saved with private permissions: {destination}")


def main():
    os.umask(0o077)
    configure_logging()
    parser = argparse.ArgumentParser(description="Circuit Perth Bot owner tools")
    parser.add_argument("--env", default=".env", help="Path to local environment file")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("setup", "run", "doctor", "discover", "healthcheck"):
        subs.add_parser(command)
    sub = subs.add_parser("backup")
    sub.add_argument("--to", type=Path, required=True)
    sub = subs.add_parser("restore")
    sub.add_argument("--from", dest="source", type=Path, required=True)
    sub = subs.add_parser("export")
    sub.add_argument("--to", type=Path, required=True)
    sub.add_argument("--user-id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(Path(args.env))
            return
        if args.command == "discover":
            asyncio.run(discover(getpass.getpass("BotFather token (hidden): ").strip()))
            return
        config = Config.load(args.env)
        if args.command == "run":
            asyncio.run(run(config))
        elif args.command == "doctor":
            sys.exit(0 if asyncio.run(doctor(config)) else 1)
        elif args.command == "healthcheck":
            heartbeat = config.data_dir / "heartbeat"
            sys.exit(0 if heartbeat.is_file() and time.time() - float(heartbeat.read_text()) < 60 else 1)
        elif args.command == "backup":
            asyncio.run(backup(config, args.to))
        elif args.command == "restore":
            restore(config, args.source)
        elif args.command == "export":
            asyncio.run(export(config, args.to, args.user_id))
    except KeyboardInterrupt:
        pass
    except (ConfigError, UserError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except TelegramError as exc:
        print(f"Telegram unavailable ({type(exc).__name__}). Check connectivity, token, chat IDs and bot permissions.", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(f"Local operation failed ({type(exc).__name__}). Check paths, permissions and available disk space.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
