from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .config import DEFAULT_SETTINGS, Config
from .db import Database, many, one


class UserError(Exception):
    """A concise error safe to display to the user."""


def now() -> int:
    return int(time.time())


def day(timestamp: int | None, timezone: str) -> str:
    return datetime.fromtimestamp(timestamp, UTC).astimezone(ZoneInfo(timezone)).strftime("%d %b %Y") if timestamp else "Unknown"


async def audit(conn, actor: int, action: str, target: str, detail: dict | None = None):
    await conn.execute("INSERT INTO audit(actor_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                       (actor, action, target, json.dumps(detail or {}), now()))


async def enqueue(conn, key: str, chat_id: int, body: str, entry_id: int | None = None):
    await conn.execute("""INSERT OR IGNORE INTO deliveries
        (entry_id,dedupe_key,chat_id,body,status,next_attempt,created_at) VALUES(?,?,?,?,'queued',?,?)""",
        (entry_id, key, chat_id, body, now(), now()))


class Service:
    def __init__(self, db: Database, config: Config):
        self.db, self.config = db, config

    async def settings(self, chat_id: int) -> dict:
        result = await self.db.kv(str(chat_id), "settings", {})
        return {**DEFAULT_SETTINGS, **result}

    async def set_setting(self, chat_id: int, key: str, value, actor: int):
        async with self.db.tx() as conn:
            row = await one(conn, "SELECT value FROM kv WHERE scope=? AND key='settings'", (str(chat_id),))
            settings = json.loads(row["value"]) if row else {}
            settings[key] = value
            await conn.execute("INSERT OR REPLACE INTO kv VALUES(?,'settings',?)", (str(chat_id), json.dumps(settings)))
            await audit(conn, actor, "setting", str(chat_id), {"key": key})

    async def session(self, user_id: int) -> dict | None:
        row = await self.db.get("SELECT * FROM sessions WHERE user_id=? AND expires_at>?", (user_id, now()))
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    async def begin(self, user_id: int, kind: str, step: str, payload: dict | None = None) -> str:
        nonce = secrets.token_hex(8)
        await self.db.run("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
                          (user_id, kind, step, json.dumps(payload or {}), nonce, now() + self.config.session_minutes * 60))
        return nonce

    async def advance(self, user_id: int, step: str, payload: dict):
        await self.db.run("UPDATE sessions SET step=?,payload=? WHERE user_id=? AND expires_at>?",
                          (step, json.dumps(payload), user_id, now()))

    async def cancel(self, user_id: int):
        await self.db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))

    async def submit(self, user_id: int, nonce: str) -> tuple[dict, bool]:
        """Atomically enforce ownership, limits, deduplication and delivery enqueue."""
        async with self.db.tx() as conn:
            prior = await one(conn, "SELECT * FROM entries WHERE source_key=? AND author_id=?", ("s:" + nonce, user_id))
            if prior:
                return prior, False
            session = await one(conn, "SELECT * FROM sessions WHERE user_id=? AND nonce=? AND step='preview' AND expires_at>?",
                                (user_id, nonce, now()))
            if not session:
                raise UserError("This form has expired or was replaced. Start the command again.")
            p = json.loads(session["payload"])
            kind = session["kind"]
            total = await one(conn, "SELECT COUNT(*) n,MAX(created_at) latest FROM entries WHERE author_id=? AND created_at>?",
                              (user_id, now() - 86400))
            if total["n"] >= self.config.daily_limit:
                raise UserError("You have reached the daily submission limit. Please try again tomorrow.")
            if total["latest"] and total["latest"] > now() - 15:
                raise UserError("Please wait 15 seconds between submissions, then press Confirm again.")
            target = p.get("target_id")
            handle = p.get("subject_handle")
            if kind in {"vouch", "report"} and target == user_id:
                raise UserError("Choose another member. You cannot vouch for or report yourself.")
            if kind in {"vouch", "report"} and not target and not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", handle or "", re.I):
                raise UserError("Enter a valid @username, or reply to a member’s group message.")
            own = await one(conn, "SELECT username FROM users WHERE id=?", (user_id,))
            if handle and own and (own["username"] or "").lower() == handle.lower():
                raise UserError("Choose another member. You cannot vouch for or report yourself.")
            if kind == "appeal":
                related = await one(conn, "SELECT * FROM entries WHERE id=? AND kind='report'", (p["related_id"],))
                if not related or related["target_id"] != user_id or related["status"] not in {"published", "resolved"}:
                    raise UserError("You can appeal a public report about your own account.")
                existing = await one(conn, "SELECT id FROM entries WHERE kind='appeal' AND related_id=? AND status='pending'", (related["id"],))
                if existing:
                    raise UserError("An appeal is already awaiting review for this report.")
            review = {"vouch": self.config.review_vouches, "report": self.config.review_reports,
                      "post": self.config.review_posts, "appeal": True}[kind]
            status = "pending" if review else ("approved" if kind == "post" else "published")
            destination = p.get("destination")
            if kind == "post" and destination not in {self.config.group_id, self.config.channel_id}:
                raise UserError("Choose Bot2Channel or Bot2Group again.")
            try:
                cur = await conn.execute("""INSERT INTO entries(kind,author_id,target_id,subject_handle,body,evidence,status,
                    destination,related_id,created_at,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (kind, user_id, target, handle, p["body"], p.get("evidence", ""), status, destination,
                     p.get("related_id"), now(), "s:" + nonce))
            except sqlite3.IntegrityError as exc:
                raise UserError("You already have an active submission of this type for that member. Use /mysubmissions to find it.") from exc
            entry_id = cur.lastrowid
            if kind == "post" and status == "approved":
                await enqueue(conn, f"post:{entry_id}", destination, "Anonymous chat\n\n" + p["body"], entry_id)
            if kind == "report" and status == "published":
                await self.enqueue_report(conn, entry_id, target, handle, p["body"])
            if kind == "vouch" and status == "published":
                await self.enqueue_vouch(conn, entry_id, target, handle, p["body"])
            await audit(conn, user_id, "submit", str(entry_id), {"kind": kind, "status": status})
            await conn.execute("DELETE FROM sessions WHERE user_id=? AND nonce=?", (user_id, nonce))
            return await one(conn, "SELECT * FROM entries WHERE id=?", (entry_id,)), True

    async def review(self, entry_id: int, actor: int, action: str, note: str) -> dict:
        async with self.db.tx() as conn:
            entry = await one(conn, "SELECT * FROM entries WHERE id=?", (entry_id,))
            if not entry:
                raise UserError("Submission not found.")
            if actor in {entry["author_id"], entry["target_id"]}:
                raise UserError("Another moderator must review submissions involving your account.")
            if entry["status"] in {"rejected", "withdrawn"}:
                raise UserError("This submission is already closed.")
            if action == "approve":
                if entry["reviewed_at"] or entry["status"] in {"resolved", "approved"}:
                    raise UserError("This submission has already been reviewed.")
                status = "approved" if entry["kind"] in {"post", "appeal"} else "published"
            elif action == "reject":
                status = "rejected"
            elif action == "resolve" and entry["kind"] == "report" and entry["status"] == "published":
                status = "resolved"
            else:
                raise UserError("This action is unavailable for that submission.")
            if action in {"reject", "resolve"} and len(note.strip()) < 5:
                raise UserError("Include a brief reason after the submission ID.")
            if entry["kind"] == "appeal" and action == "approve":
                raise UserError("Resolve or reject the original report, then close this appeal with /reject and a decision note.")
            if status in {"rejected", "resolved"}:
                await conn.execute("UPDATE deliveries SET status='canceled' WHERE entry_id=? AND status='queued'", (entry_id,))
            await conn.execute("UPDATE entries SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                               (status, actor, now(), note[:1000], entry_id))
            if entry["kind"] == "post" and status == "approved":
                await enqueue(conn, f"post:{entry_id}", entry["destination"], "Anonymous chat\n\n" + entry["body"], entry_id)
            if entry["kind"] == "report" and status == "published":
                await self.enqueue_report(conn, entry_id, entry["target_id"], entry["subject_handle"], entry["body"], reviewed=True)
            if entry["kind"] == "vouch" and status == "published":
                await self.enqueue_vouch(conn, entry_id, entry["target_id"], entry["subject_handle"], entry["body"])
            await audit(conn, actor, action, str(entry_id), {"status": status})
            return await one(conn, "SELECT * FROM entries WHERE id=?", (entry_id,))

    async def enqueue_report(self, conn, entry_id: int, target: int | None, handle: str | None, body: str, reviewed: bool = False):
        subject = f"@{handle}" if handle else f"Telegram ID {target}"
        identity = f"Telegram ID: {target}" if target else "Identity unverified · username-only record"
        state = "moderator reviewed allegation" if reviewed else "unreviewed allegation"
        text = f"Scam report #{entry_id}\nMember: {subject}\n{identity}\nAt publication: {state}\n\nReport:\n{body}\n\nSubmitted anonymously. This report is an allegation, not a finding of fact. Use /check to see its current status."
        destinations = {"channel": [self.config.channel_id], "group": [self.config.group_id],
                        "both": [self.config.channel_id, self.config.group_id]}[self.config.report_destination]
        for destination in destinations:
            await enqueue(conn, f"report:{entry_id}:{destination}", destination, text, entry_id)

    async def withdraw(self, entry_id: int, actor: int) -> dict:
        """Close a contribution and cancel its unsent publications."""
        async with self.db.tx() as conn:
            row = await one(conn, "SELECT * FROM entries WHERE id=? AND author_id=?", (entry_id, actor))
            if not row or row["status"] in {"withdrawn", "rejected", "resolved"}:
                raise UserError("Active submission not found in your account.")
            await conn.execute("UPDATE entries SET status='withdrawn' WHERE id=?", (entry_id,))
            await conn.execute("UPDATE deliveries SET status='canceled' WHERE entry_id=? AND status='queued'", (entry_id,))
            await audit(conn, actor, "withdraw", str(entry_id))
            return row

    async def enqueue_vouch(self, conn, entry_id: int, target: int | None, handle: str | None, body: str):
        subject = f"@{handle}" if handle else f"Telegram ID {target}"
        identity = f"Observed Telegram ID: {target}" if target else "Identity unverified · username-only record"
        text = f"Vouch #{entry_id}\nMember: {subject}\n{identity}\n\nExperience:\n{body}\n\nSubmitted anonymously. A vouch is a personal account, not a guarantee."
        await enqueue(conn, f"vouch:{entry_id}", self.config.channel_id, text, entry_id)

    async def dossier(self, target: int, page: int = 0) -> tuple[str, int]:
        user = await self.db.get("SELECT * FROM users WHERE id=?", (target,))
        if not user:
            raise UserError("I have not seen this member. Ask them to /start the bot, or reply to one of their group messages.")
        counts = await self.db.get("""SELECT
            SUM(kind='vouch' AND status='published') vouches,
            SUM(kind='report' AND status IN ('published','resolved')) reports,
            SUM(kind='report' AND status='published') open_reports,
            SUM(kind='report' AND status='published' AND reviewed_at IS NULL) unreviewed,
            SUM(kind='report' AND status='resolved') resolved,
            COUNT(DISTINCT CASE WHEN kind='report' AND status IN ('published','resolved') THEN author_id END) reporters
            FROM entries WHERE target_id=?""", (target,))
        counts = {k: v or 0 for k, v in counts.items()}
        total = counts["vouches"] + counts["reports"]
        pages = max(1, (total + 1) // 2)
        page = max(0, min(page, pages - 1))
        entries = await self.db.rows("""SELECT * FROM entries WHERE target_id=? AND
            ((kind='vouch' AND status='published') OR (kind='report' AND status IN ('published','resolved')))
            ORDER BY created_at DESC,id DESC LIMIT 2 OFFSET ?""", (target, page * 2))
        member = await self.db.get("SELECT * FROM members WHERE chat_id=? AND user_id=?", (self.config.group_id, target))
        handle = "@" + user["username"] if user["username"] else "No username observed"
        title = f"Circuit Perth · Member dossier\n{user['name']} · {handle}\nTelegram ID: {target}"
        text = (f"{title}\n\nVouches: {counts['vouches']}\nScam reports: {counts['reports']}"
                f" · {counts['reporters']} distinct reporters\nOpen: {counts['open_reports']}"
                f" · Unreviewed: {counts['unreviewed']} · Resolved: {counts['resolved']}"
                f"\nFirst seen by bot: {day(user['first_seen'], self.config.timezone)}")
        if member:
            text += f"\nGroup messages observed: {member['messages']} · Last group activity: {day(member['last_seen'], self.config.timezone)}"
        text += "\n\nReports are allegations. Review indicates a moderator decision, not independent proof. Vouches do not guarantee trust."
        for entry in entries:
            label = "Vouch" if entry["kind"] == "vouch" else (
                "Scam report · resolved" if entry["status"] == "resolved" else
                "Scam report · reviewed" if entry["reviewed_at"] else "Scam report · unreviewed allegation")
            text += f"\n\n#{entry['id']} · {label} · {day(entry['created_at'], self.config.timezone)}\n{entry['body']}"
        if not entries:
            text += "\n\nNo public vouches or reports recorded."
        text += f"\n\nPage {page + 1}/{pages} · Only information observed by this bot."
        return text, pages

    async def tick_timers(self):
        async with self.db.tx() as conn:
            timers = await many(conn, "SELECT * FROM timers WHERE active=1 AND next_run<=? LIMIT 20", (now(),))
            for timer in timers:
                await enqueue(conn, f"timer:{timer['id']}:{timer['next_run']}", timer["chat_id"], timer["body"])
                # Missed occurrences coalesce into one delivery after downtime.
                await conn.execute("UPDATE timers SET active=?,next_run=? WHERE id=?",
                                   (int(timer["interval_seconds"] > 0), now() + timer["interval_seconds"], timer["id"]))

    async def handle_dossier(self, handle: str, page: int = 0) -> tuple[str, int]:
        handle = handle.removeprefix("@").lower()
        count = await self.db.get("""SELECT COUNT(*) n,
            SUM(kind='vouch') vouches,SUM(kind='report') reports FROM entries
            WHERE target_id IS NULL AND subject_handle=? COLLATE NOCASE
            AND kind IN ('vouch','report') AND status IN ('published','resolved')""", (handle,))
        pages = max(1, (count["n"] + 1) // 2)
        page = max(0, min(page, pages - 1))
        rows = await self.db.rows("""SELECT * FROM entries WHERE target_id IS NULL
            AND subject_handle=? COLLATE NOCASE AND kind IN ('vouch','report') AND status IN ('published','resolved')
            ORDER BY created_at DESC,id DESC LIMIT 2 OFFSET ?""", (handle, page * 2))
        text = (f"Circuit Perth · Unverified handle dossier\n@{handle}\n\n"
                f"Vouches: {count['vouches'] or 0}\nScam reports: {count['reports'] or 0}\n\n"
                "These records were submitted against a username without a verified Telegram user ID. "
                "Usernames can change owners. Do not assume they describe the current account. Reports are allegations.")
        for row in rows:
            state = "resolved" if row["status"] == "resolved" else "reviewed" if row["reviewed_at"] else "unreviewed"
            text += f"\n\n#{row['id']} · {row['kind'].title()} · {state} · {day(row['created_at'], self.config.timezone)}\n{row['body']}"
        if not rows:
            text += "\n\nNo public handle-only records."
        return text + f"\n\nPage {page + 1}/{pages}", pages

    async def warning(self, chat_id: int, target: int, actor: int, reason: str) -> int:
        async with self.db.tx() as conn:
            await conn.execute("INSERT INTO warnings(chat_id,user_id,actor_id,reason,created_at) VALUES(?,?,?,?,?)",
                               (chat_id, target, actor, reason[:1000], now()))
            row = await one(conn, "SELECT COUNT(*) n FROM warnings WHERE chat_id=? AND user_id=? AND active=1", (chat_id, target))
            await audit(conn, actor, "warn", str(target), {"chat_id": chat_id, "count": row["n"]})
            return row["n"]
