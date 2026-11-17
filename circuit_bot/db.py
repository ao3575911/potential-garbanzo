from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT COLLATE NOCASE, name TEXT NOT NULL,
 first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, dm_started INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS users_username ON users(username COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS members (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
 first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, messages INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 kind TEXT NOT NULL CHECK(kind IN ('vouch','report','post','appeal')),
 author_id INTEGER NOT NULL REFERENCES users(id), target_id INTEGER REFERENCES users(id),
 subject_handle TEXT COLLATE NOCASE,
 body TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('pending','published','approved','rejected','withdrawn','resolved')),
 destination INTEGER, related_id INTEGER REFERENCES entries(id),
 created_at INTEGER NOT NULL, reviewed_by INTEGER, reviewed_at INTEGER, review_note TEXT NOT NULL DEFAULT '',
 source_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS entries_target ON entries(target_id,kind,status,created_at);
CREATE INDEX IF NOT EXISTS entries_author ON entries(author_id,created_at);
CREATE INDEX IF NOT EXISTS entries_queue ON entries(status,id);
CREATE UNIQUE INDEX IF NOT EXISTS unique_active_feedback ON entries(kind,author_id,target_id)
 WHERE kind IN ('vouch','report') AND status IN ('pending','published');
CREATE UNIQUE INDEX IF NOT EXISTS unique_handle_feedback ON entries(kind,author_id,subject_handle)
 WHERE kind IN ('vouch','report') AND target_id IS NULL AND status IN ('pending','published');
CREATE TABLE IF NOT EXISTS sessions (
 user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, step TEXT NOT NULL,
 payload TEXT NOT NULL, nonce TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(scope,key));
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL,
 target TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER REFERENCES entries(id),
 dedupe_key TEXT NOT NULL UNIQUE, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','failed','uncertain','canceled')),
 next_attempt INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 message_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(status,next_attempt);
CREATE TABLE IF NOT EXISTS timers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
 next_run INTEGER NOT NULL, interval_seconds INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1, author_id INTEGER NOT NULL, source_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS warnings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 actor_id INTEGER, reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS warnings_user ON warnings(chat_id,user_id,active);
CREATE TABLE IF NOT EXISTS captchas (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, answer TEXT NOT NULL, nonce TEXT NOT NULL,
 expires_at INTEGER NOT NULL, permissions TEXT NOT NULL, status TEXT NOT NULL,
 message_id INTEGER, PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS join_requests (
 chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS inbox (
 update_id INTEGER PRIMARY KEY, raw TEXT NOT NULL, status TEXT NOT NULL,
 created_at INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS inbox_queue ON inbox(status,update_id);
PRAGMA user_version=1;
"""


async def one(conn: aiosqlite.Connection, sql: str, args: tuple = ()) -> dict | None:
    async with conn.execute(sql, args) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def many(conn: aiosqlite.Connection, sql: str, args: tuple = ()) -> list[dict]:
    async with conn.execute(sql, args) as cur:
        return [dict(row) for row in await cur.fetchall()]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = await aiosqlite.connect(self.path)
        self.path.chmod(0o600)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        version = await one(self.conn, "PRAGMA user_version")
        if version["user_version"] > 1:
            raise RuntimeError("Database version is newer than this bot. Restore the matching application version.")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    @asynccontextmanager
    async def tx(self):
        async with self.lock:
            assert self.conn is not None
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def get(self, sql: str, args: tuple = ()) -> dict | None:
        async with self.lock:
            assert self.conn is not None
            return await one(self.conn, sql, args)

    async def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.lock:
            assert self.conn is not None
            return await many(self.conn, sql, args)

    async def run(self, sql: str, args: tuple = ()):
        async with self.tx() as conn:
            cur = await conn.execute(sql, args)
            return cur.lastrowid

    async def kv(self, scope: str, key: str, default: Any = None) -> Any:
        row = await self.get("SELECT value FROM kv WHERE scope=? AND key=?", (scope, key))
        return json.loads(row["value"]) if row else default

    async def set_kv(self, scope: str, key: str, value: Any):
        await self.run("INSERT INTO kv VALUES(?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value",
                       (scope, key, json.dumps(value)))

    async def audit(self, actor: int | None, action: str, target: str, detail: dict | None = None):
        await self.run("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor, action, target, json.dumps(detail or {}), int(time.time())))

    async def observe(self, user, chat_id: int | None = None, dm: bool = False, message: bool = False):
        if user.is_bot:
            return
        now = int(time.time())
        async with self.tx() as conn:
            # A newly observed username owner displaces stale username associations.
            if user.username:
                await conn.execute("UPDATE users SET username=NULL WHERE username=? COLLATE NOCASE AND id<>?",
                                   (user.username, user.id))
            await conn.execute("""INSERT INTO users VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                username=excluded.username,name=excluded.name,last_seen=excluded.last_seen,
                dm_started=MAX(users.dm_started,excluded.dm_started)""",
                (user.id, user.username, user.full_name[:160], now, now, int(dm)))
            if chat_id is not None:
                await conn.execute("""INSERT INTO members VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id)
                    DO UPDATE SET last_seen=excluded.last_seen,messages=members.messages+excluded.messages""",
                    (chat_id, user.id, now, now, int(message)))

    async def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.lock:
            assert self.conn is not None
            target = await aiosqlite.connect(destination)
            try:
                await self.conn.backup(target)
            finally:
                await target.close()
        destination.chmod(0o600)
