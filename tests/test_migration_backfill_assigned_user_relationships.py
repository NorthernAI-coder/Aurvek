import sqlite3

import pytest

from migration_backfill_assigned_user_relationships import migrate


def _create_database(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE USER_ROLES (
                id INTEGER PRIMARY KEY,
                role_name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE USERS (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                role_id INTEGER NOT NULL,
                FOREIGN KEY (role_id) REFERENCES USER_ROLES(id)
            );

            CREATE TABLE USER_DETAILS (
                user_id INTEGER PRIMARY KEY,
                created_by INTEGER,
                FOREIGN KEY (user_id) REFERENCES USERS(id),
                FOREIGN KEY (created_by) REFERENCES USERS(id)
            );

            CREATE TABLE USER_CREATOR_RELATIONSHIPS (
                user_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                relationship_type TEXT NOT NULL,
                source_type TEXT,
                source_id INTEGER,
                is_primary INTEGER NOT NULL DEFAULT 0,
                first_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, creator_id, relationship_type),
                FOREIGN KEY (user_id) REFERENCES USERS(id),
                FOREIGN KEY (creator_id) REFERENCES USERS(id)
            );

            CREATE UNIQUE INDEX idx_ucr_single_primary
            ON USER_CREATOR_RELATIONSHIPS(user_id)
            WHERE is_primary = 1;
            """
        )
        conn.executemany(
            "INSERT INTO USER_ROLES (id, role_name) VALUES (?, ?)",
            [
                (1, "admin"),
                (2, "user"),
                (3, "customer"),
            ],
        )
        conn.executemany(
            "INSERT INTO USERS (id, username, role_id) VALUES (?, ?, ?)",
            [
                (1, "admin", 1),
                (2, "creator", 2),
                (10, "new-customer", 3),
                (11, "customer-with-primary", 3),
                (12, "existing-assignment", 3),
                (13, "non-customer", 2),
                (14, "invalid-creator-role", 3),
                (15, "without-creator", 3),
                (20, "other-creator", 2),
            ],
        )
        conn.executemany(
            "INSERT INTO USER_DETAILS (user_id, created_by) VALUES (?, ?)",
            [
                (10, 2),
                (11, 1),
                (12, 2),
                (13, 2),
                (14, 12),
                (15, None),
            ],
        )
        conn.executemany(
            """
            INSERT INTO USER_CREATOR_RELATIONSHIPS
                (user_id, creator_id, relationship_type, source_type, is_primary)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (11, 20, "registered_via", "manual", 1),
                (12, 2, "assigned_by", "manual", 1),
            ],
        )


def _relationships(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            """
            SELECT user_id, creator_id, relationship_type, source_type, is_primary
            FROM USER_CREATOR_RELATIONSHIPS
            ORDER BY user_id, creator_id, relationship_type
            """
        ).fetchall()


def test_backfill_only_adds_valid_missing_assignments(tmp_path):
    db_path = tmp_path / "aurvek.db"
    _create_database(db_path)

    result = migrate(str(db_path))

    assert result == {"assigned_by": 2}
    assert _relationships(db_path) == [
        (10, 2, "assigned_by", "manual", 1),
        (11, 1, "assigned_by", "manual", 0),
        (11, 20, "registered_via", "manual", 1),
        (12, 2, "assigned_by", "manual", 1),
    ]


def test_backfill_is_idempotent(tmp_path):
    db_path = tmp_path / "aurvek.db"
    _create_database(db_path)

    first_result = migrate(str(db_path))
    rows_after_first_run = _relationships(db_path)
    second_result = migrate(str(db_path))

    assert first_result["assigned_by"] == 2
    assert second_result == {"assigned_by": 0}
    assert _relationships(db_path) == rows_after_first_run


def test_migration_skips_incomplete_schema(tmp_path):
    db_path = tmp_path / "aurvek.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE USERS (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="Missing required tables"):
        migrate(str(db_path))
