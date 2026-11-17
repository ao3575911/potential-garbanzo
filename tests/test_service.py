import asyncio
from dataclasses import replace

import pytest

from circuit_bot.db import Database
from circuit_bot.service import Service, UserError


async def ready(service, user=1, kind="vouch", target=2, handle="bobuser", **extra):
    payload = {"target_id": target, "subject_handle": handle, "body": "A clear account of my experience.", **extra}
    return await service.begin(user, kind, "preview", payload)


async def test_atomic_submission_replay_and_author_binding(service, db):
    nonce = await ready(service)
    a, b = await asyncio.gather(service.submit(1, nonce), service.submit(1, nonce))
    assert a[0]["id"] == b[0]["id"]
    assert sorted([a[1], b[1]]) == [False, True]
    assert (await db.get("SELECT COUNT(*) n FROM entries"))["n"] == 1
    with pytest.raises(UserError, match="expired"):
        await service.submit(3, nonce)


async def test_immediate_report_queues_channel_only(service, db, config):
    nonce = await ready(service, kind="report")
    entry, _ = await service.submit(1, nonce)
    assert entry["status"] == "published"
    deliveries = await db.rows("SELECT * FROM deliveries")
    assert {d["chat_id"] for d in deliveries} == {config.channel_id}
    for delivery in deliveries:
        assert "Scam report #" in delivery["body"]
        assert "@bobuser" in delivery["body"]
        assert "unreviewed allegation" in delivery["body"]
        assert "alice" not in delivery["body"]


async def test_post_only_selected_destination(service, db, config):
    nonce = await ready(service, kind="post", target=None, handle=None, destination=config.channel_id)
    entry, _ = await service.submit(1, nonce)
    rows = await db.rows("SELECT * FROM deliveries")
    assert len(rows) == 1 and rows[0]["chat_id"] == config.channel_id
    assert rows[0]["body"].startswith("Anonymous chat\n\n")
    assert entry["status"] == "approved"


async def test_review_switches_hold_outgoing(db, config):
    service = Service(db, replace(config, review_reports=True, review_posts=True))
    nonce = await ready(service, kind="report")
    entry, _ = await service.submit(1, nonce)
    assert entry["status"] == "pending"
    assert not await db.rows("SELECT * FROM deliveries")
    await service.review(entry["id"], 99, "approve", "")
    assert len(await db.rows("SELECT * FROM deliveries")) == 1


async def test_self_vouches_and_reports_rejected(service):
    for kind in ("report", "vouch"):
        nonce = await ready(service, kind=kind, target=1, handle="alice")
        with pytest.raises(UserError, match="yourself"):
            await service.submit(1, nonce)


async def test_unique_pair_across_new_forms(service, db):
    nonce = await ready(service)
    await service.submit(1, nonce)
    await db.run("UPDATE entries SET created_at=created_at-20")
    nonce = await ready(service)
    with pytest.raises(UserError, match="already have"):
        await service.submit(1, nonce)


async def test_unknown_handle_kept_separate(service, db):
    nonce = await ready(service, kind="report", target=None, handle="unknown_user")
    entry, _ = await service.submit(1, nonce)
    assert entry["target_id"] is None
    text, pages = await service.handle_dossier("unknown_user")
    assert "Scam reports: 1" in text and "Usernames can change owners" in text
    text, _ = await service.dossier(2)
    assert "Scam reports: 0" in text


async def test_counts_and_withdrawal_exclude_unpublished(service, db):
    nonce = await ready(service, kind="report")
    entry, _ = await service.submit(1, nonce)
    nonce = await ready(service, user=3, kind="vouch")
    await service.submit(3, nonce)
    text, _ = await service.dossier(2)
    assert "Vouches: 1" in text and "Scam reports: 1" in text and "Unreviewed: 1" in text
    await service.withdraw(entry["id"], 1)
    text, _ = await service.dossier(2)
    assert "Scam reports: 0" in text
    assert all(d["status"] == "canceled" for d in await db.rows("SELECT * FROM deliveries WHERE entry_id=?", (entry["id"],)))


async def test_vouch_immediately_publishes_only_to_channel(service, db, config):
    nonce = await ready(service)
    entry, _ = await service.submit(1, nonce)
    deliveries = await db.rows("SELECT * FROM deliveries")
    assert len(deliveries) == 1 and deliveries[0]["chat_id"] == config.channel_id
    assert deliveries[0]["body"].startswith(f"Vouch #{entry['id']}\nMember: @bobuser")
    assert "Experience:" in deliveries[0]["body"] and "alice" not in deliveries[0]["body"]
    await service.submit(1, nonce)
    assert len(await db.rows("SELECT * FROM deliveries")) == 1


async def test_held_vouch_is_published_to_channel_after_approval(db, config):
    service = Service(db, replace(config, review_vouches=True))
    entry, _ = await service.submit(1, await ready(service))
    assert not await db.rows("SELECT * FROM deliveries")
    await service.review(entry["id"], 99, "approve", "")
    delivery = await db.get("SELECT * FROM deliveries")
    assert delivery["chat_id"] == config.channel_id and delivery["body"].startswith("Vouch #")


async def test_cannot_withdraw_someone_elses_entry(service):
    entry, _ = await service.submit(1, await ready(service))
    with pytest.raises(UserError, match="not found"):
        await service.withdraw(entry["id"], 3)


async def test_review_no_self_decisions_and_resolution(service):
    entry, _ = await service.submit(1, await ready(service, kind="report"))
    for reviewer in (1, 2):
        with pytest.raises(UserError, match="Another moderator"):
            await service.review(entry["id"], reviewer, "reject", "bad report")
    await service.review(entry["id"], 99, "resolve", "Issue resolved")
    text, _ = await service.dossier(2)
    assert "Scam reports: 1" in text and "Resolved: 1" in text and "Open: 0" in text


async def test_report_duplicate_does_not_duplicate_delivery(service, db):
    nonce = await ready(service, kind="report")
    await service.submit(1, nonce)
    await service.submit(1, nonce)
    assert len(await db.rows("SELECT * FROM deliveries")) == 1


async def test_daily_limit_survives_new_sessions(service, config, db):
    limited = Service(db, replace(config, daily_limit=1))
    await limited.submit(1, await ready(limited))
    await db.run("UPDATE entries SET created_at=created_at-20")
    with pytest.raises(UserError, match="daily"):
        await limited.submit(1, await ready(limited, kind="report"))


async def test_expired_session_rejected(service, db):
    nonce = await ready(service)
    await db.run("UPDATE sessions SET expires_at=0")
    with pytest.raises(UserError, match="expired"):
        await service.submit(1, nonce)


async def test_session_and_database_backup_survive_restart(service, db, config, tmp_path):
    nonce = await ready(service)
    backup = tmp_path / "copy.sqlite3"
    await db.backup(backup)
    restored = Database(backup)
    await restored.open()
    try:
        resumed = Service(restored, config)
        assert (await resumed.session(1))["nonce"] == nonce
        entry, _ = await resumed.submit(1, nonce)
        assert entry["kind"] == "vouch"
    finally:
        await restored.close()


async def test_timer_coalesces_and_deduplicates(service, db, config):
    await db.run("INSERT INTO timers(chat_id,body,next_run,interval_seconds,author_id,source_key) VALUES(?,?,0,60,99,'timer1')", (config.group_id, "Scheduled announcement"))
    await asyncio.gather(service.tick_timers(), service.tick_timers())
    assert len(await db.rows("SELECT * FROM deliveries")) == 1


async def test_pagination_keeps_all_feedback(service, db):
    for index in range(5):
        await db.run("INSERT INTO entries(kind,author_id,target_id,body,status,created_at,source_key) VALUES('vouch',1,2,?,'resolved',?,?)", (f"Historic {index}", index, f"historic{index}"))
    # Reports retain resolved history and may have multiple independent incidents.
    await db.run("UPDATE entries SET kind='report'")
    seen = set()
    for page in range(3):
        text, pages = await service.dossier(2, page)
        assert pages == 3
        seen |= {i for i in range(5) if f"Historic {i}" in text}
    assert seen == set(range(5))
