import sqlite3

import pytest

from migration_discount_scopes import migrate


def _create_legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE USERS (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL
        );
        CREATE TABLE DISCOUNTS (
            code TEXT PRIMARY KEY,
            discount_value REAL,
            active INTEGER,
            validity_date TEXT,
            usage_count INTEGER,
            unlimited_usage INTEGER DEFAULT 0,
            unlimited_validity INTEGER DEFAULT 0,
            created_by_user_id INTEGER
        );
        INSERT INTO USERS (id, username) VALUES (1, 'user-one');
        INSERT INTO DISCOUNTS
            (code, discount_value, active, usage_count,
             unlimited_usage, unlimited_validity)
        VALUES ('LEGACY100', 100, 1, 10, 0, 1);
        """
    )
    conn.commit()
    conn.close()


def test_migration_keeps_existing_codes_marketplace_only(tmp_path):
    db_path = tmp_path / "discounts.db"
    _create_legacy_db(db_path)

    result = migrate(str(db_path))

    assert result == {
        "scope_added": True,
        "wallet_grant_added": True,
        "redemptions_created": True,
    }
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT scope, wallet_grant_amount FROM DISCOUNTS WHERE code = 'LEGACY100'"
        ).fetchone()
        columns = {
            item[1] for item in conn.execute("PRAGMA table_info(DISCOUNT_REDEMPTIONS)")
        }
    finally:
        conn.close()

    assert row == ("marketplace", None)
    assert {
        "discount_code",
        "user_id",
        "purpose",
        "grant_amount",
        "transaction_reference",
        "redeemed_at",
    }.issubset(columns)


def test_migration_is_idempotent_and_enforces_wallet_grant_and_redemption_uniqueness(tmp_path):
    db_path = tmp_path / "discounts.db"
    _create_legacy_db(db_path)
    migrate(str(db_path))
    second = migrate(str(db_path))
    assert second == {
        "scope_added": False,
        "wallet_grant_added": False,
        "redemptions_created": False,
    }

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            """
            INSERT INTO DISCOUNTS
                (code, discount_value, active, usage_count,
                 unlimited_usage, unlimited_validity, scope, wallet_grant_amount)
            VALUES ('WALLET10', 0, 1, NULL, 1, 1, 'wallet', 10)
            """
        )
        conn.execute(
            """
            INSERT INTO DISCOUNT_REDEMPTIONS
                (discount_code, user_id, purpose, grant_amount)
            VALUES ('WALLET10', 1, 'wallet_credit', 10)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO DISCOUNT_REDEMPTIONS
                    (discount_code, user_id, purpose, grant_amount)
                VALUES ('WALLET10', 1, 'wallet_credit', 10)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO DISCOUNTS
                    (code, discount_value, active, scope, wallet_grant_amount)
                VALUES ('TOO_LARGE', 0, 1, 'wallet', 501)
                """
            )
    finally:
        conn.close()
