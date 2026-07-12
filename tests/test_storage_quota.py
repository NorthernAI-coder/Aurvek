import sqlite3
import sys
from pathlib import Path

import aiosqlite
import pytest

import file_storage
import migration_storage_quotas
import reconcile_generated_media
import storage_quota


def _create_quota_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE USERS (id INTEGER PRIMARY KEY, username TEXT NOT NULL);
        CREATE TABLE USER_DETAILS (
            user_id INTEGER PRIMARY KEY,
            storage_quota_bytes INTEGER
        );
        CREATE TABLE CONVERSATIONS (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES USERS(id)
        );
        CREATE TABLE SYSTEM_CONFIG (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT,
            updated_at TIMESTAMP
        );
        CREATE TABLE FILE_BLOBS (
            id INTEGER PRIMARY KEY,
            size_bytes INTEGER NOT NULL
        );
        CREATE TABLE FILE_ATTACHMENTS (
            id INTEGER PRIMARY KEY,
            blob_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE GENERATED_MEDIA_FILES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('image', 'video', 'pdf', 'mp3', 'wav')),
            rel_path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
            FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE
        );
        """
    )


def test_storage_migration_is_idempotent_and_enforces_non_negative_overrides(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "quota.db"
    data_root = tmp_path / "data"
    media_path = (
        data_root
        / "users"
        / "aa"
        / "bb"
        / "hash"
        / "files"
        / "000"
        / "0001"
        / "img"
        / "bot"
        / "backfill.webp"
    )
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"12345")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE USERS (id INTEGER PRIMARY KEY);
        CREATE TABLE USER_DETAILS (user_id INTEGER PRIMARY KEY);
        CREATE TABLE CONVERSATIONS (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);
        CREATE TABLE SYSTEM_CONFIG (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT,
            updated_at TIMESTAMP
        );
        INSERT INTO USERS (id) VALUES (1);
        INSERT INTO USER_DETAILS (user_id) VALUES (1);
        INSERT INTO CONVERSATIONS (id, user_id) VALUES (1, 1);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(migration_storage_quotas, "DB_PATH", str(db_path))
    monkeypatch.setattr(migration_storage_quotas, "DATA_ROOT", str(data_root))
    migration_storage_quotas.migrate()
    migration_storage_quotas.migrate()

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(USER_DETAILS)")}
    assert "storage_quota_bytes" in columns
    assert conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key = 'storage_quota_default_bytes'"
    ).fetchone() == (str(storage_quota.DEFAULT_QUOTA_BYTES),)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name = 'GENERATED_MEDIA_FILES'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT conversation_id, size_bytes FROM GENERATED_MEDIA_FILES"
    ).fetchall() == [(1, 5)]
    assert conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key = ?",
        (migration_storage_quotas.BACKFILL_CONFIG_KEY,),
    ).fetchone() == ("1",)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE USER_DETAILS SET storage_quota_bytes = -1 WHERE user_id = 1")
    conn.close()


@pytest.mark.asyncio
async def test_quota_usage_is_deduplicated_and_combines_generated_media(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "usage.db")
    await conn.executescript(
        """
        CREATE TABLE USER_DETAILS (user_id INTEGER PRIMARY KEY, storage_quota_bytes INTEGER);
        CREATE TABLE SYSTEM_CONFIG (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE FILE_BLOBS (id INTEGER PRIMARY KEY, size_bytes INTEGER NOT NULL);
        CREATE TABLE FILE_ATTACHMENTS (
            id INTEGER PRIMARY KEY,
            blob_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE GENERATED_MEDIA_FILES (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL
        );
        INSERT INTO USER_DETAILS VALUES (1, 10);
        INSERT INTO FILE_BLOBS VALUES (1, 6), (2, 3);
        INSERT INTO FILE_ATTACHMENTS VALUES (1, 1, 1, 'active');
        INSERT INTO FILE_ATTACHMENTS VALUES (2, 1, 1, 'pending');
        INSERT INTO GENERATED_MEDIA_FILES VALUES (1, 1, 2);
        """
    )

    assert await storage_quota.get_uploads_usage_bytes(conn, 1) == 6
    assert await storage_quota.get_total_usage_bytes(conn, 1) == 8
    await storage_quota.ensure_known_growth_fits(conn, 1, 2)
    with pytest.raises(storage_quota.StorageQuotaExceededError):
        await storage_quota.ensure_known_growth_fits(conn, 1, 3)
    await storage_quota.ensure_upload_fits(conn, 1, 1, 6)
    with pytest.raises(storage_quota.StorageQuotaExceededError):
        await storage_quota.ensure_upload_fits(conn, 1, 2, 3)
    await conn.close()


@pytest.mark.asyncio
async def test_generated_media_ledger_uses_canonical_paths_and_upserts(tmp_path):
    db_path = tmp_path / "ledger.db"
    sync_conn = sqlite3.connect(db_path)
    _create_quota_schema(sync_conn)
    sync_conn.executescript(
        """
        INSERT INTO USERS VALUES (1, 'owner');
        INSERT INTO USER_DETAILS VALUES (1, 1000);
        INSERT INTO CONVERSATIONS VALUES (1, 1);
        """
    )
    sync_conn.commit()
    sync_conn.close()

    media_path = (
        tmp_path
        / "data"
        / "users"
        / "aa"
        / "bb"
        / "hash"
        / "files"
        / "000"
        / "0001"
        / "img"
        / "bot"
        / "image.webp"
    )
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"12345")

    conn = await aiosqlite.connect(db_path)
    await storage_quota.record_generated_file(conn, 1, "image", str(media_path), 5)
    await storage_quota.record_generated_file(conn, 1, "image", str(media_path), 7)
    await conn.commit()
    row = await (
        await conn.execute(
            "SELECT rel_path, size_bytes FROM GENERATED_MEDIA_FILES"
        )
    ).fetchone()
    assert row == (
        "aa/bb/hash/files/000/0001/img/bot/image.webp",
        7,
    )

    with pytest.raises(ValueError, match="not under data/users"):
        await storage_quota.record_generated_file(
            conn, 1, "image", str(tmp_path / "outside.webp"), 1
        )

    assert await storage_quota.delete_generated_file_rows(conn, [str(media_path)]) == 1
    await conn.commit()
    await conn.close()


@pytest.mark.asyncio
async def test_deleting_generated_image_variants_frees_quota_usage(tmp_path):
    import app as app_module

    db_path = tmp_path / "image-delete.db"
    sync_conn = sqlite3.connect(db_path)
    _create_quota_schema(sync_conn)
    sync_conn.executescript(
        """
        INSERT INTO USERS VALUES (1, 'owner');
        INSERT INTO USER_DETAILS VALUES (1, 1000);
        INSERT INTO CONVERSATIONS VALUES (1, 1);
        """
    )
    sync_conn.commit()
    sync_conn.close()

    user_dir = tmp_path / "data" / "users" / "aa" / "bb" / "hash"
    image_dir = user_dir / "files" / "000" / "0001" / "img" / "bot"
    image_dir.mkdir(parents=True)
    thumbnail = image_dir / "image_256.webp"
    fullsize = image_dir / "image_fullsize.webp"
    thumbnail.write_bytes(b"123")
    fullsize.write_bytes(b"12345")

    conn = await aiosqlite.connect(db_path)
    await storage_quota.record_generated_file(conn, 1, "image", str(thumbnail), 3)
    await storage_quota.record_generated_file(conn, 1, "image", str(fullsize), 5)
    await conn.commit()
    assert await storage_quota.get_generated_usage_bytes(conn, 1) == 8

    result = await app_module.delete_file_variants(
        [thumbnail, fullsize], user_dir, conn
    )
    await conn.commit()

    assert result == (2, 0, 2)
    assert not thumbnail.exists()
    assert not fullsize.exists()
    assert await storage_quota.get_generated_usage_bytes(conn, 1) == 0
    await conn.close()


def test_reconcile_skips_conversations_whose_user_no_longer_exists(tmp_path):
    conn = sqlite3.connect(tmp_path / "reconcile.db")
    _create_quota_schema(conn)
    conn.execute("INSERT INTO USERS VALUES (1, 'valid')")
    conn.execute("INSERT INTO CONVERSATIONS VALUES (1, 1)")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO CONVERSATIONS VALUES (2, 999)")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    valid_rel = "aa/bb/valid/files/000/0001/img/bot/valid.webp"
    orphan_rel = "aa/bb/orphan/files/000/0002/img/bot/orphan.webp"
    disk = {
        "known": {
            valid_rel: {
                "rel": valid_rel,
                "size": 5,
                "kind": "image",
                "conv_id": 1,
            },
            orphan_rel: {
                "rel": orphan_rel,
                "size": 7,
                "kind": "image",
                "conv_id": 2,
            },
        },
        "unknown_files": [],
        "anomalies": [],
    }
    owners = reconcile_generated_media.load_conversation_owners(conn)
    assert owners == {1: 1}
    drift = reconcile_generated_media.diff(disk, {}, owners, str(tmp_path))
    assert [item["rel"] for item in drift["missing_insertable"]] == [valid_rel]
    assert [item["rel"] for item in drift["orphaned_files"]] == [orphan_rel]

    reconcile_generated_media.apply_fix(conn, drift)
    assert conn.execute(
        "SELECT conversation_id, rel_path FROM GENERATED_MEDIA_FILES"
    ).fetchall() == [(1, valid_rel)]
    conn.close()


def test_reconcile_refuses_missing_users_tree_without_mutating_ledger(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "reconcile-missing-root.db"
    conn = sqlite3.connect(db_path)
    _create_quota_schema(conn)
    conn.execute("INSERT INTO USERS VALUES (1, 'owner')")
    conn.execute("INSERT INTO CONVERSATIONS VALUES (1, 1)")
    conn.execute(
        """
        INSERT INTO GENERATED_MEDIA_FILES
            (user_id, conversation_id, kind, rel_path, size_bytes)
        VALUES (1, 1, 'image', 'aa/bb/hash/files/000/0001/img/bot/a.webp', 5)
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reconcile_generated_media.py",
            "--db",
            str(db_path),
            "--data-root",
            str(tmp_path / "missing-data"),
            "--fix",
        ],
    )
    assert reconcile_generated_media.main() == 1

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM GENERATED_MEDIA_FILES"
    ).fetchone() == (1,)
    conn.close()


@pytest.mark.asyncio
async def test_file_ingest_rejects_and_prunes_over_quota_blob(
    mock_db, db_path, tmp_path, monkeypatch
):
    monkeypatch.setattr(file_storage, "FILE_BLOB_ROOT", tmp_path / "file_blobs")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        INSERT INTO USERS (id, username, role_id) VALUES (1, 'owner', 1);
        INSERT INTO USER_DETAILS (user_id, storage_quota_bytes) VALUES (1, 4);
        INSERT INTO CONVERSATIONS (id, user_id, chat_name, role_id)
        VALUES (1, 1, 'quota', 1);
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(storage_quota.StorageQuotaExceededError):
        await file_storage.create_pending_text_attachment(
            user_id=1,
            conversation_id=1,
            text_content="12345",
            filename="too-large.txt",
        )

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM FILE_BLOBS").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS").fetchone() == (0,)
    conn.execute("UPDATE USER_DETAILS SET storage_quota_bytes = 5 WHERE user_id = 1")
    conn.commit()
    conn.close()

    first = await file_storage.create_pending_text_attachment(
        user_id=1,
        conversation_id=1,
        text_content="12345",
        filename="first.txt",
    )
    second = await file_storage.create_pending_text_attachment(
        user_id=1,
        conversation_id=1,
        text_content="12345",
        filename="second.txt",
    )
    assert first.blob_id == second.blob_id

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM FILE_BLOBS").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM FILE_ATTACHMENTS").fetchone() == (2,)
    conn.close()
