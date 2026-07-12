import sqlite3

import pytest

from migration_phone_verification_challenges import migrate


def _create_users_table(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE USERS (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO USERS (id, username) VALUES (1, 'actor')")


def test_migration_creates_challenge_schema_and_indexes(tmp_path):
    db_path = tmp_path / "aurvek.db"
    _create_users_table(db_path)

    result = migrate(str(db_path))

    assert result == {"created": True}
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]: row[2]
            for row in conn.execute(
                "PRAGMA table_info(PHONE_VERIFICATION_CHALLENGES)"
            )
        }
        indexes = {
            row[1]
            for row in conn.execute(
                "PRAGMA index_list(PHONE_VERIFICATION_CHALLENGES)"
            )
        }
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(PHONE_VERIFICATION_CHALLENGES)"
        ).fetchall()

    assert {
        "id",
        "actor_user_id",
        "phone_number",
        "purpose",
        "request_ip",
        "status",
        "verification_attempts",
        "provider_sid",
        "created_at",
        "expires_at",
        "approved_at",
        "consumed_at",
        "last_attempt_at",
    } <= columns.keys()
    assert {
        "idx_phone_verification_actor_created",
        "idx_phone_verification_phone_created",
        "idx_phone_verification_ip_created",
        "idx_phone_verification_status_expires",
    } <= indexes
    assert any(row[2] == "USERS" and row[3] == "actor_user_id" for row in foreign_keys)


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "aurvek.db"
    _create_users_table(db_path)

    first = migrate(str(db_path))
    second = migrate(str(db_path))

    assert first == {"created": True}
    assert second == {"created": False}


def test_migration_constraints_reject_unknown_purpose_and_status(tmp_path):
    db_path = tmp_path / "aurvek.db"
    _create_users_table(db_path)
    migrate(str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO PHONE_VERIFICATION_CHALLENGES (
                    id, actor_user_id, phone_number, purpose, request_ip,
                    status, created_at, expires_at
                ) VALUES ('bad-purpose', 1, '+12025550101', 'other',
                          '127.0.0.1', 'pending', 1, 2)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO PHONE_VERIFICATION_CHALLENGES (
                    id, actor_user_id, phone_number, purpose, request_ip,
                    status, created_at, expires_at
                ) VALUES ('bad-status', 1, '+12025550101', 'create_user',
                          '127.0.0.1', 'other', 1, 2)
                """
            )


def test_challenges_are_deleted_with_actor(tmp_path):
    db_path = tmp_path / "aurvek.db"
    _create_users_table(db_path)
    migrate(str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO PHONE_VERIFICATION_CHALLENGES (
                id, actor_user_id, phone_number, purpose, request_ip,
                status, created_at, expires_at
            ) VALUES ('challenge', 1, '+12025550101', 'create_user',
                      '127.0.0.1', 'pending', 1, 2)
            """
        )
        conn.execute("DELETE FROM USERS WHERE id = 1")
        count = conn.execute(
            "SELECT COUNT(*) FROM PHONE_VERIFICATION_CHALLENGES"
        ).fetchone()[0]

    assert count == 0
