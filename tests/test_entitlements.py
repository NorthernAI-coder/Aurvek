from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from migration_entitlements import migrate
from marketplace.services.entitlements import (
    ASSET_TYPE_PACK,
    ASSET_TYPE_PROMPT,
    grant_pack_entitlement,
    refund_entitlement,
    user_has_pack_access,
    user_has_prompt_access,
)


def _create_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE USERS (
            id INTEGER PRIMARY KEY,
            username TEXT
        );

        CREATE TABLE PROMPTS (
            id INTEGER PRIMARY KEY,
            name TEXT,
            created_by_user_id INTEGER,
            public INTEGER DEFAULT 0,
            purchase_price REAL
        );

        CREATE TABLE PROMPT_PERMISSIONS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            permission_level TEXT NOT NULL
        );

        CREATE TABLE PROMPT_PURCHASES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_user_id INTEGER NOT NULL,
            prompt_id INTEGER NOT NULL,
            amount DECIMAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            payment_method TEXT,
            payment_reference TEXT,
            discount_code TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE PACKS (
            id INTEGER PRIMARY KEY,
            name TEXT,
            created_by_user_id INTEGER,
            status TEXT DEFAULT 'published',
            is_public INTEGER DEFAULT 1
        );

        CREATE TABLE PACK_ITEMS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id INTEGER NOT NULL,
            prompt_id INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1,
            disable_at DATETIME,
            display_order INTEGER DEFAULT 0
        );

        CREATE TABLE PACK_ACCESS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            granted_via TEXT NOT NULL,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP DEFAULT NULL
        );

        CREATE TABLE PACK_PURCHASES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_user_id INTEGER NOT NULL,
            pack_id INTEGER NOT NULL,
            amount DECIMAL NOT NULL,
            currency TEXT DEFAULT 'USD',
            payment_method TEXT,
            payment_reference TEXT,
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()


def _seed_users_and_assets(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        INSERT INTO USERS (id, username) VALUES (1, 'creator'), (2, 'buyer'), (3, 'editor');
        INSERT INTO PROMPTS (id, name, created_by_user_id) VALUES (10, 'Prompt', 1), (11, 'Pack Prompt', 1);
        INSERT INTO PACKS (id, name, created_by_user_id) VALUES (20, 'Pack', 1);
        INSERT INTO PACK_ITEMS (pack_id, prompt_id, is_active, display_order) VALUES (20, 11, 1, 1);
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def entitlement_db(tmp_path):
    db_path = tmp_path / "entitlements.db"
    _create_schema(db_path)
    _seed_users_and_assets(db_path)
    return db_path


def test_backfill_prompt_purchase_skips_matching_access_permission(entitlement_db):
    conn = sqlite3.connect(entitlement_db)
    conn.execute(
        """INSERT INTO PROMPT_PURCHASES
           (buyer_user_id, prompt_id, amount, payment_method, payment_reference, status)
           VALUES (2, 10, 9.99, 'stripe', 'cs_test_prompt', 'completed')"""
    )
    conn.execute(
        "INSERT INTO PROMPT_PERMISSIONS (prompt_id, user_id, permission_level) VALUES (10, 2, 'access')"
    )
    conn.commit()
    conn.close()

    first = migrate(str(entitlement_db))
    second = migrate(str(entitlement_db))

    conn = sqlite3.connect(entitlement_db)
    rows = conn.execute(
        "SELECT asset_type, asset_id, source, source_ref_type, source_ref_id, status FROM ENTITLEMENTS"
    ).fetchall()
    conn.close()

    assert first["prompt_purchases"] == 1
    assert second == {
        "prompt_purchases": 0,
        "prompt_permission_access": 0,
        "pack_purchases": 0,
        "pack_access": 0,
    }
    assert rows == [("prompt", 10, "purchase", "stripe_session", "cs_test_prompt", "active")]


def test_backfill_pack_purchase_requires_active_pack_access(entitlement_db):
    conn = sqlite3.connect(entitlement_db)
    conn.execute(
        """INSERT INTO PACK_PURCHASES
           (buyer_user_id, pack_id, amount, payment_method, payment_reference, status)
           VALUES (2, 20, 19.99, 'stripe', 'cs_pack_no_access', 'completed')"""
    )
    conn.commit()
    conn.close()

    counts = migrate(str(entitlement_db))

    conn = sqlite3.connect(entitlement_db)
    count = conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS WHERE asset_type = 'pack'").fetchone()[0]
    conn.close()

    assert counts["pack_purchases"] == 0
    assert counts["pack_access"] == 0
    assert count == 0


def test_migration_drops_prompt_purchase_user_prompt_unique_index(entitlement_db):
    migrate(str(entitlement_db))

    conn = sqlite3.connect(entitlement_db)
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(PROMPT_PURCHASES)").fetchall()
    }
    conn.execute(
        """INSERT INTO PROMPT_PURCHASES
           (buyer_user_id, prompt_id, amount, payment_method, payment_reference, status)
           VALUES (2, 10, 9.99, 'stripe', 'cs_rebuy_1', 'refunded')"""
    )
    conn.execute(
        """INSERT INTO PROMPT_PURCHASES
           (buyer_user_id, prompt_id, amount, payment_method, payment_reference, status)
           VALUES (2, 10, 9.99, 'stripe', 'cs_rebuy_2', 'completed')"""
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM PROMPT_PURCHASES WHERE buyer_user_id = 2 AND prompt_id = 10"
    ).fetchone()[0]
    conn.close()

    assert "idx_prompt_purchases_unique" not in indexes
    assert count == 2


def test_backfill_pack_access_links_purchase_and_preserves_expiry(entitlement_db):
    conn = sqlite3.connect(entitlement_db)
    conn.execute(
        """INSERT INTO PACK_PURCHASES
           (buyer_user_id, pack_id, amount, payment_method, payment_reference, status)
           VALUES (2, 20, 19.99, 'stripe', 'cs_pack_access', 'completed')"""
    )
    conn.execute(
        """INSERT INTO PACK_ACCESS (pack_id, user_id, granted_via, expires_at)
           VALUES (20, 2, 'purchase', '2099-01-01 00:00:00')"""
    )
    conn.commit()
    conn.close()

    counts = migrate(str(entitlement_db))

    conn = sqlite3.connect(entitlement_db)
    row = conn.execute(
        """SELECT asset_type, asset_id, source, source_ref_type, source_ref_id, expires_at, status
           FROM ENTITLEMENTS WHERE asset_type = 'pack'"""
    ).fetchone()
    conn.close()

    assert counts["pack_purchases"] == 1
    assert counts["pack_access"] == 0
    assert row == ("pack", 20, "purchase", "stripe_session", "cs_pack_access", "2099-01-01 00:00:00", "active")


def test_backfill_expired_pack_access_does_not_grant(entitlement_db):
    conn = sqlite3.connect(entitlement_db)
    conn.execute(
        """INSERT INTO PACK_ACCESS (pack_id, user_id, granted_via, expires_at)
           VALUES (20, 2, 'admin_grant', '2000-01-01 00:00:00')"""
    )
    conn.commit()
    conn.close()

    counts = migrate(str(entitlement_db))

    conn = sqlite3.connect(entitlement_db)
    count = conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS WHERE asset_type = 'pack'").fetchone()[0]
    conn.close()

    assert counts["pack_access"] == 0
    assert count == 0


@pytest.mark.asyncio
async def test_prompt_refund_does_not_fall_back_to_legacy_access_permission(entitlement_db):
    conn = sqlite3.connect(entitlement_db)
    conn.execute(
        """INSERT INTO PROMPT_PURCHASES
           (buyer_user_id, prompt_id, amount, payment_method, payment_reference, status)
           VALUES (2, 10, 9.99, 'stripe', 'cs_refund_prompt', 'completed')"""
    )
    conn.execute(
        "INSERT INTO PROMPT_PERMISSIONS (prompt_id, user_id, permission_level) VALUES (10, 2, 'access')"
    )
    conn.commit()
    conn.close()
    migrate(str(entitlement_db))

    async with aiosqlite.connect(entitlement_db) as db:
        db.row_factory = aiosqlite.Row
        assert await user_has_prompt_access(db, user_id=2, prompt_id=10)
        await refund_entitlement(
            db,
            user_id=2,
            asset_type=ASSET_TYPE_PROMPT,
            asset_id=10,
            source_ref_type="stripe_session",
            source_ref_id="cs_refund_prompt",
        )
        await db.commit()
        assert not await user_has_prompt_access(db, user_id=2, prompt_id=10)


@pytest.mark.asyncio
async def test_pack_entitlement_inherits_prompt_access_from_active_items(entitlement_db):
    migrate(str(entitlement_db))

    async with aiosqlite.connect(entitlement_db) as db:
        db.row_factory = aiosqlite.Row
        await grant_pack_entitlement(
            db,
            user_id=2,
            pack_id=20,
            source="free_claim",
            source_ref_type="test",
            source_ref_id="pack-20",
        )
        await db.commit()

        assert await user_has_pack_access(db, user_id=2, pack_id=20)
        assert await user_has_prompt_access(db, user_id=2, prompt_id=11)

        await db.execute("UPDATE PACK_ITEMS SET is_active = 0 WHERE pack_id = 20 AND prompt_id = 11")
        await db.commit()

        assert await user_has_pack_access(db, user_id=2, pack_id=20)
        assert not await user_has_prompt_access(db, user_id=2, prompt_id=11)


@pytest.mark.asyncio
async def test_owner_and_editor_permissions_are_not_backfilled_as_entitlements(entitlement_db):
    conn = sqlite3.connect(entitlement_db)
    conn.execute(
        "INSERT INTO PROMPT_PERMISSIONS (prompt_id, user_id, permission_level) VALUES (10, 1, 'owner')"
    )
    conn.execute(
        "INSERT INTO PROMPT_PERMISSIONS (prompt_id, user_id, permission_level) VALUES (10, 3, 'edit')"
    )
    conn.commit()
    conn.close()

    migrate(str(entitlement_db))

    conn = sqlite3.connect(entitlement_db)
    count = conn.execute("SELECT COUNT(*) FROM ENTITLEMENTS").fetchone()[0]
    conn.close()
    assert count == 0

    async with aiosqlite.connect(entitlement_db) as db:
        db.row_factory = aiosqlite.Row
        assert await user_has_prompt_access(db, user_id=1, prompt_id=10)
        assert await user_has_prompt_access(db, user_id=3, prompt_id=10)


@pytest.mark.asyncio
async def test_pack_refund_removes_inherited_prompt_access(entitlement_db):
    migrate(str(entitlement_db))

    async with aiosqlite.connect(entitlement_db) as db:
        db.row_factory = aiosqlite.Row
        await grant_pack_entitlement(
            db,
            user_id=2,
            pack_id=20,
            source="purchase",
            source_ref_type="stripe_session",
            source_ref_id="cs_pack_refund",
        )
        await db.commit()

        assert await user_has_prompt_access(db, user_id=2, prompt_id=11)
        await refund_entitlement(
            db,
            user_id=2,
            asset_type=ASSET_TYPE_PACK,
            asset_id=20,
            source_ref_type="stripe_session",
            source_ref_id="cs_pack_refund",
        )
        await db.commit()

        assert not await user_has_pack_access(db, user_id=2, pack_id=20)
        assert not await user_has_prompt_access(db, user_id=2, prompt_id=11)


@pytest.mark.asyncio
async def test_direct_entitlement_to_deleted_prompt_fails_closed(entitlement_db):
    migrate(str(entitlement_db))

    async with aiosqlite.connect(entitlement_db) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """INSERT INTO ENTITLEMENTS
               (user_id, asset_type, asset_id, source, source_ref_type, source_ref_id, status)
               VALUES (2, 'prompt', 999, 'migration', 'test', 'missing-prompt', 'active')"""
        )
        await db.commit()

        assert not await user_has_prompt_access(db, user_id=2, prompt_id=999)
