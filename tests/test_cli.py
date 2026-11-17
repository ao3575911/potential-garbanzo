import json

import pytest

from circuit_bot.cli import backup, exclusive_write, export, restore
from circuit_bot.db import Database
from circuit_bot.service import UserError


async def test_scoped_export_excludes_other_profiles(db, config, tmp_path):
    path = tmp_path / "export.json"
    await export(config, path, 1)
    data = json.loads(path.read_text())
    assert [user["id"] for user in data["users"]] == [1]
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        exclusive_write(path, "overwrite")


async def test_invalid_export_id_never_exports_all(db, config, tmp_path):
    path = tmp_path / "invalid-export.json"
    with pytest.raises(UserError, match="positive"):
        await export(config, path, 0)
    assert not path.exists()


async def test_backup_and_restore_preserve_records(config, tmp_path):
    # Restore is deliberately exercised with the original connection closed.
    from telegram import User

    db = Database(config.db_path)
    await db.open()
    await db.observe(User(555, "A member", False))
    path = tmp_path / "backups" / "snapshot.sqlite3"
    await db.close()
    await backup(config, path)
    restore(config, path)
    restored = Database(config.db_path)
    await restored.open()
    try:
        assert (await restored.get("SELECT id FROM users"))["id"] == 555
        assert list(config.data_dir.glob("before-restore-*.sqlite3"))
    finally:
        await restored.close()


def test_restore_rejects_invalid_files(config, tmp_path):
    path = tmp_path / "invalid.sqlite3"
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(id INTEGER)")
    with pytest.raises(UserError, match="validation"):
        restore(config, path)
