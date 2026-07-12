import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import orjson
import pytest
import pytest_asyncio
from starlette.responses import StreamingResponse
from unittest.mock import AsyncMock

from billing import usage_reservations
from billing.usage_reservations import (
    BillingReservationError,
    BillingLimitExceededError,
    InsufficientBalanceError,
    accumulate_ai_reservation_usage,
    billing_account_guard,
    claim_fixed_usage_provider,
    mark_fixed_usage_provider_succeeded,
    complete_ai_reservation_settlement,
    estimate_structured_billing_tokens,
    estimate_structured_usage_tokens,
    extend_ai_reservation,
    get_variable_billing_rates,
    prepare_ai_reservation_settlement,
    refund_fixed_usage,
    reconcile_stale_usage_reservations,
    reserve_ai_usage,
    reserve_fixed_usage,
    serialize_user_billing_response,
    serialize_user_billing_stream,
    settle_accumulated_ai_reservation_usage,
    settle_ai_reservation_components,
    settle_fixed_usage,
    settle_fixed_usage_in_transaction,
)
from migration_billing_usage_reservations import migrate


_BASE_SCHEMA = """
CREATE TABLE USERS (
    id INTEGER PRIMARY KEY
);

CREATE TABLE SERVICES (
    id INTEGER PRIMARY KEY
);

CREATE TABLE USER_DETAILS (
    user_id INTEGER PRIMARY KEY,
    balance REAL DEFAULT 0,
    total_cost REAL DEFAULT 0,
    total_image_cost REAL DEFAULT 0,
    total_stt_cost REAL DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    input_token_cost REAL DEFAULT 0,
    output_token_cost REAL DEFAULT 0,
    tokens_spent INTEGER DEFAULT 0,
    billing_account_id INTEGER,
    billing_limit REAL,
    billing_limit_action TEXT DEFAULT 'block',
    billing_current_month_spent REAL DEFAULT 0,
    billing_month_reset_date TEXT,
    billing_auto_refill_amount REAL DEFAULT 10,
    billing_max_limit REAL,
    billing_auto_refill_count INTEGER DEFAULT 0
);

CREATE TABLE SERVICE_USAGE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    usage_quantity REAL NOT NULL,
    cost REAL NOT NULL
);

CREATE TABLE USAGE_DAILY (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date DATE NOT NULL,
    type TEXT NOT NULL,
    operations INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    units REAL DEFAULT 0,
    total_cost REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, date, type)
);
"""


@pytest_asyncio.fixture
async def billing_db(monkeypatch, tmp_path):
    db_path = tmp_path / "billing.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_BASE_SCHEMA)
        conn.executemany("INSERT INTO USERS (id) VALUES (?)", [(1,), (2,), (3,)])
        conn.executemany("INSERT INTO SERVICES (id) VALUES (?)", [(10,), (11,)])
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (1, 1.0)"
        )
        conn.commit()

    first_result = migrate(str(db_path))
    second_result = migrate(str(db_path))
    assert first_result["created"] is True
    assert second_result["created"] is False

    @asynccontextmanager
    async def get_connection(readonly=False):
        mode = "ro" if readonly else "rwc"
        conn = await aiosqlite.connect(
            f"file:{db_path}?mode={mode}",
            uri=True,
            timeout=5,
        )
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(usage_reservations, "get_db_connection", get_connection)
    yield db_path


def _fetchone(db_path, query, parameters=()):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(query, parameters).fetchone()


def test_migration_rebuilds_legacy_fixed_only_table(tmp_path):
    db_path = tmp_path / "legacy-reservations.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE USERS (id INTEGER PRIMARY KEY);
            CREATE TABLE SERVICES (id INTEGER PRIMARY KEY);
            INSERT INTO USERS (id) VALUES (1);
            INSERT INTO SERVICES (id) VALUES (10);
            CREATE TABLE BILLING_USAGE_RESERVATIONS (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                billing_account_id INTEGER NOT NULL,
                purpose TEXT NOT NULL CHECK(purpose IN ('image', 'stt')),
                service_id INTEGER NOT NULL,
                usage_quantity REAL NOT NULL CHECK(usage_quantity > 0),
                amount REAL NOT NULL CHECK(amount > 0),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'settled', 'refunded')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP,
                refunded_at TIMESTAMP
            );
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, purpose, service_id,
                usage_quantity, amount
            ) VALUES ('legacy-image', 1, 1, 'image', 10, 1, 0.2);
            """
        )

    result = migrate(str(db_path))

    assert result["legacy_rebuilt"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """
            SELECT purpose, amount, billing_month,
                   accumulated_input_tokens, accumulated_output_tokens,
                   billing_limit_delta, billing_refill_count_delta,
                   provider_succeeded_at
            FROM BILLING_USAGE_RESERVATIONS
            WHERE id = 'legacy-image'
            """
        ).fetchone() == (
            "image",
            0.2,
            datetime.now(timezone.utc).strftime("%Y-%m"),
            0,
            0,
            0.0,
            0,
            None,
        )
        conn.execute(
            """
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, purpose, amount
            ) VALUES ('new-ai', 1, 1, 'ai', 0.4)
            """
        )
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'BILLING_USAGE_RESERVATIONS'"
        ).fetchone()[0]
    assert "'ai'" in ddl


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overdraw_one_account(billing_db):
    async def reserve():
        return await reserve_fixed_usage(
            user_id=1,
            purpose="stt",
            amount=0.75,
            service_id=10,
            usage_quantity=1,
        )

    results = await asyncio.gather(reserve(), reserve(), return_exceptions=True)

    reservation_ids = [result for result in results if isinstance(result, str)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(reservation_ids) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InsufficientBalanceError)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.25)
    assert _fetchone(
        billing_db,
        "SELECT COUNT(*) FROM BILLING_USAGE_RESERVATIONS WHERE status = 'active'",
    )[0] == 1


@pytest.mark.asyncio
async def test_self_referential_billing_account_behaves_as_direct_billing(
    billing_db,
):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET billing_account_id = user_id,
                billing_limit = 0.5,
                billing_current_month_spent = 0,
                billing_month_reset_date = ?
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.commit()

    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.4,
        service_id=10,
        usage_quantity=1,
    )
    assert _fetchone(
        billing_db,
        "SELECT balance, billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((0.6, 0.0))

    assert await refund_fixed_usage(reservation_id) is True
    assert _fetchone(
        billing_db,
        "SELECT balance, billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((1.0, 0.0))


@pytest.mark.asyncio
async def test_reservation_enforces_auto_refill_max_even_below_current_limit(
    billing_db,
):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 100,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 40,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 10,
                billing_max_limit = 50
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 100)"
        )
        conn.commit()

    with pytest.raises(BillingLimitExceededError):
        await reserve_fixed_usage(
            user_id=1,
            purpose="image",
            amount=20,
            service_id=10,
            usage_quantity=1,
        )

    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(100)
    assert _fetchone(
        billing_db,
        "SELECT billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(40)


@pytest.mark.asyncio
async def test_reservation_extension_enforces_auto_refill_max(
    billing_db,
):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 100,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 40,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 10,
                billing_max_limit = 50
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 100)"
        )
        conn.commit()

    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=5)
    with pytest.raises(BillingLimitExceededError):
        await extend_ai_reservation(
            reservation_id=reservation_id,
            user_id=1,
            additional_amount=6,
        )

    assert _fetchone(
        billing_db,
        "SELECT amount FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    )[0] == pytest.approx(5)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(95)
    assert _fetchone(
        billing_db,
        "SELECT billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(45)


@pytest.mark.asyncio
async def test_reservations_are_safe_across_fresh_event_loops(billing_db):
    def reserve_in_worker_loop():
        async def reserve():
            return await reserve_fixed_usage(
                user_id=1,
                purpose="stt",
                amount=0.75,
                service_id=10,
                usage_quantity=1,
            )

        try:
            return asyncio.run(reserve())
        except BaseException as exc:
            return exc

    results = await asyncio.gather(
        asyncio.to_thread(reserve_in_worker_loop),
        asyncio.to_thread(reserve_in_worker_loop),
    )

    assert sum(isinstance(result, str) for result in results) == 1
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], InsufficientBalanceError)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.25)


def test_migration_backfills_month_when_adding_column_without_rebuild(tmp_path):
    db_path = tmp_path / "modern-reservations.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE USERS (id INTEGER PRIMARY KEY);
            CREATE TABLE SERVICES (id INTEGER PRIMARY KEY);
            INSERT INTO USERS (id) VALUES (1);
            CREATE TABLE BILLING_USAGE_RESERVATIONS (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                billing_account_id INTEGER NOT NULL,
                purpose TEXT NOT NULL
                    CHECK(purpose IN ('ai', 'image', 'stt', 'video')),
                service_id INTEGER,
                usage_quantity REAL,
                amount REAL NOT NULL CHECK(amount > 0),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'settled', 'refunded')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP,
                refunded_at TIMESTAMP
            );
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, purpose, amount, created_at
            ) VALUES ('active-ai', 1, 1, 'ai', 0.2, '2026-06-15 12:00:00');
            """
        )

    result = migrate(str(db_path))

    assert result["legacy_rebuilt"] is False
    assert result["billing_month_added"] is True
    assert result["billing_limit_delta_added"] is True
    assert result["billing_refill_count_delta_added"] is True
    assert result["provider_succeeded_at_added"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            """
            SELECT billing_month, billing_limit_delta,
                   billing_refill_count_delta, provider_succeeded_at
            FROM BILLING_USAGE_RESERVATIONS
            WHERE id = 'active-ai'
            """
        ).fetchone() == ("2026-06", 0.0, 0, None)


@pytest.mark.asyncio
async def test_team_reservation_charges_payer_and_refund_is_idempotent(billing_db):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 0.5,
                billing_limit_action = 'block',
                billing_month_reset_date = ?
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 1.0)"
        )
        conn.commit()

    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.4,
        service_id=11,
        usage_quantity=1,
    )

    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(0.6)
    assert _fetchone(
        billing_db,
        "SELECT billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.4)

    with pytest.raises(BillingLimitExceededError):
        await reserve_fixed_usage(
            user_id=1,
            purpose="stt",
            amount=0.2,
            service_id=10,
            usage_quantity=1,
        )

    # A later team-account change must not leave the original reservation in
    # the member's monthly-spend counter.
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            "UPDATE USER_DETAILS SET billing_account_id = 3 WHERE user_id = 1"
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (3, 1.0)"
        )
        conn.commit()

    assert await refund_fixed_usage(reservation_id) is True
    assert await refund_fixed_usage(reservation_id) is True
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(1.0)
    assert _fetchone(
        billing_db,
        "SELECT billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_team_auto_refill_can_use_a_partial_final_increment(billing_db):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 0.45,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 0.44,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 0.1,
                billing_max_limit = 0.5
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 1.0)"
        )
        conn.commit()

    await reserve_fixed_usage(
        user_id=1,
        purpose="stt",
        amount=0.05,
        service_id=10,
        usage_quantity=1,
    )

    assert _fetchone(
        billing_db,
        "SELECT billing_limit, billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((0.5, 0.49))


@pytest.mark.asyncio
async def test_refund_reverses_only_its_auto_refill_adjustment(billing_db):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 0.2,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 0.15,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 0.1,
                billing_max_limit = 1.0,
                billing_auto_refill_count = 0
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 10.0)"
        )
        conn.commit()

    first_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.25,
        service_id=11,
        usage_quantity=1,
    )
    second_id = await reserve_fixed_usage(
        user_id=1,
        purpose="stt",
        amount=0.25,
        service_id=10,
        usage_quantity=1,
    )

    assert _fetchone(
        billing_db,
        """
        SELECT billing_limit_delta, billing_refill_count_delta
        FROM BILLING_USAGE_RESERVATIONS WHERE id = ?
        """,
        (first_id,),
    ) == pytest.approx((0.2, 2))
    assert _fetchone(
        billing_db,
        """
        SELECT billing_limit_delta, billing_refill_count_delta
        FROM BILLING_USAGE_RESERVATIONS WHERE id = ?
        """,
        (second_id,),
    ) == pytest.approx((0.3, 3))

    assert await refund_fixed_usage(first_id) is True
    assert _fetchone(
        billing_db,
        """
        SELECT billing_limit, billing_current_month_spent,
               billing_auto_refill_count
        FROM USER_DETAILS WHERE user_id = 1
        """,
    ) == pytest.approx((0.5, 0.4, 3))

    assert await refund_fixed_usage(second_id) is True
    assert _fetchone(
        billing_db,
        """
        SELECT billing_limit, billing_current_month_spent,
               billing_auto_refill_count
        FROM USER_DETAILS WHERE user_id = 1
        """,
    ) == pytest.approx((0.2, 0.15, 0))
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_ai_prepare_reverts_refills_from_reservation_and_extensions(
    billing_db,
):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 0.2,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 0.15,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 0.1,
                billing_max_limit = 1.0,
                billing_auto_refill_count = 0
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 10.0)"
        )
        conn.commit()

    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.25)
    await extend_ai_reservation(
        reservation_id=reservation_id,
        user_id=1,
        additional_amount=0.35,
    )
    assert _fetchone(
        billing_db,
        """
        SELECT amount, billing_limit_delta, billing_refill_count_delta
        FROM BILLING_USAGE_RESERVATIONS WHERE id = ?
        """,
        (reservation_id,),
    ) == pytest.approx((0.6, 0.6, 6))

    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        credit = await prepare_ai_reservation_settlement(
            conn,
            reservation_id=reservation_id,
            user_id=1,
        )
        cursor = await conn.execute(
            """
            SELECT billing_limit, billing_current_month_spent,
                   billing_auto_refill_count
            FROM USER_DETAILS WHERE user_id = 1
            """
        )
        assert tuple(await cursor.fetchone()) == pytest.approx((0.2, 0.15, 0))
        assert await complete_ai_reservation_settlement(conn, credit) == 0
        await conn.commit()

    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_old_month_refund_does_not_remove_current_month_refills(billing_db):
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 0.2,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 0.15,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 0.1,
                billing_max_limit = 1.0
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 10.0)"
        )
        conn.commit()

    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.25,
        service_id=11,
        usage_quantity=1,
    )
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET billing_month = '2000-01'
            WHERE id = ?
            """,
            (reservation_id,),
        )
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET billing_limit = 0.7,
                billing_current_month_spent = 0.3,
                billing_auto_refill_count = 3
            WHERE user_id = 1
            """
        )
        conn.commit()

    assert await refund_fixed_usage(reservation_id) is True
    assert _fetchone(
        billing_db,
        """
        SELECT billing_limit, billing_current_month_spent,
               billing_auto_refill_count
        FROM USER_DETAILS WHERE user_id = 1
        """,
    ) == pytest.approx((0.7, 0.3, 3))


@pytest.mark.asyncio
async def test_consume_token_applies_all_required_auto_refill_increments(
    billing_db,
    monkeypatch,
):
    import common

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 0.2,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 0.1,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 0.1,
                billing_max_limit = 0.7,
                billing_auto_refill_count = 0
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 1.0)"
        )
        conn.commit()

    async def pricing_config():
        return {
            "margin_free": 0.0,
            "margin_paid": 0.0,
            "margin_personal": 0.0,
            "commission": 0.0,
        }

    monkeypatch.setattr(common, "get_pricing_config", pricing_config)
    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT 1")
        assert await common.consume_token(
            1,
            0,
            0,
            0,
            0,
            conn,
            cursor,
            override_api_cost=0.55,
        ) is True
        await conn.commit()

    assert _fetchone(
        billing_db,
        """
        SELECT billing_limit, billing_current_month_spent,
               billing_auto_refill_count
        FROM USER_DETAILS WHERE user_id = 1
        """,
    ) == pytest.approx((0.7, 0.65, 5))
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_consume_token_can_charge_the_payer_fixed_by_a_reservation(
    billing_db,
    monkeypatch,
):
    import common

    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            "UPDATE USER_DETAILS SET billing_account_id = 3 WHERE user_id = 1"
        )
        conn.executemany(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (?, ?)",
            [(2, 1.0), (3, 1.0)],
        )
        conn.commit()

    async def pricing_config():
        return {
            "margin_free": 0.0,
            "margin_paid": 0.0,
            "margin_personal": 0.0,
            "commission": 0.0,
        }

    monkeypatch.setattr(common, "get_pricing_config", pricing_config)
    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT 1")
        assert await common.consume_token(
            1,
            0,
            0,
            0,
            0,
            conn,
            cursor,
            override_api_cost=0.25,
            billing_account_id_override=2,
        ) is True
        await conn.commit()

    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(0.75)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 3",
    )[0] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_consume_token_enforces_auto_refill_max_below_current_limit(
    billing_db,
    monkeypatch,
):
    import common

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = 0,
                billing_account_id = 2,
                billing_limit = 100,
                billing_limit_action = 'auto_refill',
                billing_current_month_spent = 40,
                billing_month_reset_date = ?,
                billing_auto_refill_amount = 10,
                billing_max_limit = 50
            WHERE user_id = 1
            """,
            (current_month,),
        )
        conn.execute(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (2, 100)"
        )
        conn.commit()

    async def pricing_config():
        return {
            "margin_free": 0.0,
            "margin_paid": 0.0,
            "margin_personal": 0.0,
            "commission": 0.0,
        }

    monkeypatch.setattr(common, "get_pricing_config", pricing_config)
    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT 1")
        assert await common.consume_token(
            1,
            0,
            0,
            0,
            0,
            conn,
            cursor,
            override_api_cost=20,
        ) is False
        await conn.rollback()

    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(100)
    assert _fetchone(
        billing_db,
        "SELECT billing_current_month_spent FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(40)


@pytest.mark.asyncio
async def test_settlement_records_usage_once_and_cannot_then_refund(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )

    assert await settle_fixed_usage(reservation_id) is True
    assert await settle_fixed_usage(reservation_id) is True
    assert await refund_fixed_usage(reservation_id) is False

    assert _fetchone(
        billing_db,
        "SELECT balance, total_cost, total_image_cost FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((0.8, 0.2, 0.2))
    assert _fetchone(
        billing_db,
        "SELECT COUNT(*), SUM(cost), SUM(usage_quantity) FROM SERVICE_USAGE",
    ) == pytest.approx((1, 0.2, 1.0))
    assert _fetchone(
        billing_db,
        "SELECT operations, units, total_cost FROM USAGE_DAILY WHERE user_id = 1 AND type = 'image'",
    ) == pytest.approx((1, 1.0, 0.2))


@pytest.mark.asyncio
async def test_fixed_settlement_rolls_back_with_its_calling_transaction(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )

    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        assert await settle_fixed_usage_in_transaction(
            conn,
            reservation_id,
            expected_user_id=1,
        ) is True
        await conn.rollback()

    assert _fetchone(
        billing_db,
        "SELECT status FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    ) == ("active",)
    assert _fetchone(
        billing_db,
        "SELECT total_cost, total_image_cost FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((0.0, 0.0))
    assert _fetchone(
        billing_db,
        "SELECT COUNT(*) FROM SERVICE_USAGE",
    ) == (0,)


@pytest.mark.asyncio
async def test_video_settlement_records_total_and_daily_usage(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="video",
        amount=0.5,
        service_id=11,
        usage_quantity=1,
    )

    assert await settle_fixed_usage(reservation_id) is True
    assert _fetchone(
        billing_db,
        "SELECT balance, total_cost, total_image_cost FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((0.5, 0.5, 0.0))
    assert _fetchone(
        billing_db,
        "SELECT operations, units, total_cost FROM USAGE_DAILY WHERE user_id = 1 AND type = 'video'",
    ) == pytest.approx((1, 1.0, 0.5))


@pytest.mark.asyncio
async def test_ai_reservation_restores_max_and_commits_only_actual_charge(billing_db):
    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.2)

    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        credit = await prepare_ai_reservation_settlement(
            conn,
            reservation_id=reservation_id,
            user_id=1,
        )
        await conn.execute(
            "UPDATE USER_DETAILS SET balance = balance - 0.3 WHERE user_id = 1"
        )
        actual = await complete_ai_reservation_settlement(conn, credit)
        await conn.commit()

    assert actual == pytest.approx(0.3)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.7)
    assert _fetchone(
        billing_db,
        "SELECT status, settled_amount FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    ) == pytest.approx(("settled", 0.3))
    assert await refund_fixed_usage(reservation_id) is False


@pytest.mark.asyncio
async def test_ai_reservation_carries_pre_tool_provider_usage_to_settlement(
    billing_db,
):
    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
    await accumulate_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=1,
        input_tokens=120,
        output_tokens=30,
    )

    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        credit = await prepare_ai_reservation_settlement(
            conn,
            reservation_id=reservation_id,
            user_id=1,
        )
        assert credit.accumulated_input_tokens == 120
        assert credit.accumulated_output_tokens == 30
        await conn.rollback()
    assert await refund_fixed_usage(reservation_id) is True


@pytest.mark.asyncio
async def test_ai_component_idempotency_key_prevents_duplicate_accumulation(
    billing_db,
):
    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
    component = {
        "input_tokens": 120,
        "output_tokens": 30,
        "input_cost_per_million": 0,
        "output_cost_per_million": 0,
        "prompt_id": None,
        "byok": False,
        "override_api_cost": 0.2,
        "idempotency_key": "provider-session:terminal",
    }

    for _ in range(2):
        await accumulate_ai_reservation_usage(
            reservation_id=reservation_id,
            user_id=1,
            input_tokens=120,
            output_tokens=30,
            component=component,
        )

    row = _fetchone(
        billing_db,
        """
        SELECT accumulated_input_tokens, accumulated_output_tokens,
               accumulated_components
        FROM BILLING_USAGE_RESERVATIONS WHERE id = ?
        """,
        (reservation_id,),
    )
    assert row[:2] == (120, 30)
    assert len(orjson.loads(row[2])) == 1


def test_usage_fallback_is_distinct_from_conservative_reservation_bound():
    payload = {"role": "user", "content": "x" * 4000}

    reservation_bound = estimate_structured_billing_tokens(payload)
    usage_fallback = estimate_structured_usage_tokens(payload)

    assert reservation_bound > usage_fallback * 3


@pytest.mark.asyncio
async def test_ai_reservation_extension_and_partial_capture_are_atomic(
    billing_db,
    monkeypatch,
):
    import common

    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.4)
    await accumulate_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=1,
        input_tokens=120,
        output_tokens=30,
    )
    await extend_ai_reservation(
        reservation_id=reservation_id,
        user_id=1,
        additional_amount=0.3,
    )

    consumed = {}

    async def consume(
        user_id,
        input_tokens,
        output_tokens,
        input_cost,
        output_cost,
        conn,
        cursor,
        **kwargs,
    ):
        consumed.update(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await conn.execute(
            "UPDATE USER_DETAILS SET balance = balance - 0.25 WHERE user_id = ?",
            (user_id,),
        )
        return True

    monkeypatch.setattr(common, "consume_token", consume)
    assert await settle_accumulated_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=1,
        input_cost_per_million=1.0,
        output_cost_per_million=2.0,
        prompt_id=None,
        byok=False,
    ) is True

    assert consumed == {"input_tokens": 120, "output_tokens": 30}
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.75)
    assert _fetchone(
        billing_db,
        "SELECT amount, settled_amount, status FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    ) == pytest.approx((0.7, 0.25, "settled"))


@pytest.mark.asyncio
async def test_multi_component_partial_capture_uses_each_model_rate(
    billing_db,
    monkeypatch,
):
    import common

    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
    calls = []

    async def consume(
        user_id,
        input_tokens,
        output_tokens,
        input_cost,
        output_cost,
        conn,
        cursor,
        **kwargs,
    ):
        calls.append((input_tokens, output_tokens, input_cost, output_cost, kwargs["byok"]))
        await conn.execute(
            "UPDATE USER_DETAILS SET balance = balance - 0.1 WHERE user_id = ?",
            (user_id,),
        )
        return True

    monkeypatch.setattr(common, "consume_token", consume)
    assert await settle_ai_reservation_components(
        reservation_id=reservation_id,
        user_id=1,
        prompt_id=None,
        components=[
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 2.0,
                "byok": False,
            },
            {
                "input_tokens": 80,
                "output_tokens": 10,
                "input_cost_per_million": 3.0,
                "output_cost_per_million": 4.0,
                "byok": True,
            },
        ],
    ) is True

    assert calls == [
        (100, 20, 1.0, 2.0, False),
        (80, 10, 3.0, 4.0, True),
    ]
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_ai_settlement_rolls_back_if_actual_exceeds_reserved_max(billing_db):
    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.4)

    async with usage_reservations.get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        credit = await prepare_ai_reservation_settlement(
            conn,
            reservation_id=reservation_id,
            user_id=1,
        )
        await conn.execute(
            "UPDATE USER_DETAILS SET balance = balance - 0.5 WHERE user_id = 1"
        )
        with pytest.raises(BillingReservationError):
            await complete_ai_reservation_settlement(conn, credit)
        await conn.rollback()

    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.6)
    assert await refund_fixed_usage(reservation_id) is True
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_variable_rates_match_paid_prompt_and_referral_formula(monkeypatch):
    import common

    monkeypatch.setattr(
        common,
        "get_pricing_config",
        AsyncMock(
            return_value={
                "margin_free": 0.2,
                "margin_paid": 0.1,
                "margin_personal": 0.15,
            }
        ),
    )
    monkeypatch.setattr(
        common,
        "get_prompt_pricing_info",
        AsyncMock(
            return_value={
                "is_paid": True,
                "markup_per_mtokens": 3.0,
                "created_by_user_id": 99,
            }
        ),
    )
    monkeypatch.setattr(
        common,
        "get_user_referral_info",
        AsyncMock(
            return_value={
                "created_by": 88,
                "referral_markup_per_mtokens": 2.0,
            }
        ),
    )

    rates = await get_variable_billing_rates(
        user_id=1,
        prompt_id=7,
        input_cost_per_million=10.0,
        output_cost_per_million=20.0,
        byok=False,
    )

    assert rates.input_per_token == pytest.approx((10.0 * 1.1 + 5.0) / 1_000_000)
    assert rates.output_per_token == pytest.approx((20.0 * 1.1 + 5.0) / 1_000_000)

    byok_rates = await get_variable_billing_rates(
        user_id=1,
        prompt_id=7,
        input_cost_per_million=10.0,
        output_cost_per_million=20.0,
        byok=True,
    )
    assert byok_rates.input_per_token == pytest.approx(5.0 / 1_000_000)
    assert byok_rates.output_per_token == pytest.approx(5.0 / 1_000_000)


@pytest.mark.asyncio
async def test_concurrent_settle_and_refund_leave_one_consistent_outcome(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="stt",
        amount=0.3,
        service_id=10,
        usage_quantity=2,
    )

    await asyncio.gather(
        settle_fixed_usage(reservation_id),
        refund_fixed_usage(reservation_id),
    )

    status = _fetchone(
        billing_db,
        "SELECT status FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    )[0]
    balance = _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0]
    usage_count = _fetchone(
        billing_db,
        "SELECT COUNT(*) FROM SERVICE_USAGE",
    )[0]
    assert (status, balance, usage_count) in {
        ("settled", 0.7, 1),
        ("refunded", 1.0, 0),
    }


@pytest.mark.asyncio
async def test_only_one_image_worker_can_claim_a_reservation(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )

    claims = await asyncio.gather(
        claim_fixed_usage_provider(
            reservation_id,
            purpose="image",
            user_id=1,
        ),
        claim_fixed_usage_provider(
            reservation_id,
            purpose="image",
            user_id=1,
        ),
    )

    assert sorted(claims) == [False, True]
    assert await refund_fixed_usage(reservation_id) is True


@pytest.mark.asyncio
async def test_success_marker_is_a_durable_refund_barrier(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )

    assert await mark_fixed_usage_provider_succeeded(
        reservation_id,
        purpose="image",
        user_id=1,
    ) is True
    assert await mark_fixed_usage_provider_succeeded(
        reservation_id,
        purpose="image",
        user_id=1,
    ) is True
    assert await refund_fixed_usage(reservation_id) is False
    assert _fetchone(
        billing_db,
        """
        SELECT status, provider_succeeded_at
        FROM BILLING_USAGE_RESERVATIONS WHERE id = ?
        """,
        (reservation_id,),
    )[0] == "active"
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_stale_successful_fixed_reservation_is_settled(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )
    assert await mark_fixed_usage_provider_succeeded(
        reservation_id,
        purpose="image",
        user_id=1,
    ) is True
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET provider_started_at = datetime('now', '-16 minutes')
            WHERE id = ?
            """,
            (reservation_id,),
        )
        conn.commit()

    assert await reconcile_stale_usage_reservations() == 1
    assert _fetchone(
        billing_db,
        "SELECT status, settled_amount FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    ) == pytest.approx(("settled", 0.2))
    assert _fetchone(
        billing_db,
        "SELECT balance, total_cost, total_image_cost FROM USER_DETAILS WHERE user_id = 1",
    ) == pytest.approx((0.8, 0.2, 0.2))
    assert _fetchone(
        billing_db,
        "SELECT COUNT(*), SUM(cost) FROM SERVICE_USAGE",
    ) == pytest.approx((1, 0.2))


@pytest.mark.asyncio
async def test_stale_claimed_image_reservation_is_refunded(billing_db):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET provider_started_at = datetime('now', '-16 minutes')
            WHERE id = ?
            """,
            (reservation_id,),
        )
        conn.commit()

    assert await reconcile_stale_usage_reservations() == 1
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_stale_ai_reservation_settles_persisted_components(
    billing_db,
    monkeypatch,
):
    import common

    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
    await accumulate_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=1,
        input_tokens=120,
        output_tokens=30,
        component={
            "input_tokens": 120,
            "output_tokens": 30,
            "input_cost_per_million": 1.0,
            "output_cost_per_million": 2.0,
            "prompt_id": None,
            "byok": False,
        },
    )
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET created_at = datetime('now', '-13 hours')
            WHERE id = ?
            """,
            (reservation_id,),
        )
        conn.commit()

    async def consume(
        user_id,
        input_tokens,
        output_tokens,
        input_cost,
        output_cost,
        conn,
        cursor,
        **kwargs,
    ):
        assert (input_tokens, output_tokens) == (120, 30)
        await conn.execute(
            "UPDATE USER_DETAILS SET balance = balance - 0.25 WHERE user_id = ?",
            (user_id,),
        )
        return True

    monkeypatch.setattr(common, "consume_token", consume)

    assert await reconcile_stale_usage_reservations() == 1
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.75)
    assert _fetchone(
        billing_db,
        "SELECT status, settled_amount FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    ) == pytest.approx(("settled", 0.25))


@pytest.mark.asyncio
async def test_stale_ai_reservation_settles_against_its_original_payer(
    billing_db,
    monkeypatch,
):
    import common

    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            "UPDATE USER_DETAILS SET balance = 0, billing_account_id = 2 WHERE user_id = 1"
        )
        conn.executemany(
            "INSERT INTO USER_DETAILS (user_id, balance) VALUES (?, ?)",
            [(2, 1.0), (3, 1.0)],
        )
        conn.commit()

    reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.4)
    await accumulate_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=1,
        input_tokens=10,
        output_tokens=5,
        component={
            "input_tokens": 10,
            "output_tokens": 5,
            "input_cost_per_million": 0,
            "output_cost_per_million": 0,
            "prompt_id": None,
            "byok": False,
            "override_api_cost": 0.1,
        },
    )
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            "UPDATE USER_DETAILS SET billing_account_id = 3 WHERE user_id = 1"
        )
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET created_at = datetime('now', '-13 hours')
            WHERE id = ?
            """,
            (reservation_id,),
        )
        conn.commit()

    async def pricing_config():
        return {
            "margin_free": 0.0,
            "margin_paid": 0.0,
            "margin_personal": 0.0,
            "commission": 0.0,
        }

    monkeypatch.setattr(common, "get_pricing_config", pricing_config)

    assert await reconcile_stale_usage_reservations() == 1
    assert await reconcile_stale_usage_reservations() == 0
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 2",
    )[0] == pytest.approx(0.9)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 3",
    )[0] == pytest.approx(1.0)
    assert _fetchone(
        billing_db,
        "SELECT status, settled_amount FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    ) == pytest.approx(("settled", 0.1))


@pytest.mark.asyncio
async def test_invalid_stale_ai_row_does_not_block_other_reconciliation(billing_db):
    poisoned_id = await reserve_ai_usage(user_id=1, maximum_amount=0.4)
    image_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET created_at = datetime('now', '-13 hours'),
                accumulated_components =
                    '[{"input_tokens":"invalid","output_tokens":1}]'
            WHERE id = ?
            """,
            (poisoned_id,),
        )
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET created_at = datetime('now', '-16 minutes')
            WHERE id = ?
            """,
            (image_id,),
        )
        conn.commit()

    assert await reconcile_stale_usage_reservations() == 1
    assert _fetchone(
        billing_db,
        "SELECT status FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (poisoned_id,),
    )[0] == "active"
    assert _fetchone(
        billing_db,
        "SELECT status FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (image_id,),
    )[0] == "refunded"


@pytest.mark.asyncio
async def test_reconcile_does_not_refund_an_image_claimed_while_it_waits(
    billing_db,
    monkeypatch,
):
    reservation_id = await reserve_fixed_usage(
        user_id=1,
        purpose="image",
        amount=0.2,
        service_id=11,
        usage_quantity=1,
    )
    with sqlite3.connect(billing_db) as conn:
        conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET created_at = datetime('now', '-16 minutes')
            WHERE id = ?
            """,
            (reservation_id,),
        )
        conn.commit()

    original_get_connection = usage_reservations.get_db_connection
    reconcile_write_attempted = asyncio.Event()

    class _TrackedConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        async def execute(self, query, parameters=()):
            if query.strip().upper().startswith("BEGIN IMMEDIATE"):
                reconcile_write_attempted.set()
            return await self._conn.execute(query, parameters)

    @asynccontextmanager
    async def tracked_get_connection(readonly=False):
        async with original_get_connection(readonly=readonly) as conn:
            yield _TrackedConnection(conn)

    async with original_get_connection() as claim_conn:
        await claim_conn.execute("BEGIN IMMEDIATE")
        await claim_conn.execute(
            """
            UPDATE BILLING_USAGE_RESERVATIONS
            SET provider_started_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'active'
            """,
            (reservation_id,),
        )
        monkeypatch.setattr(
            usage_reservations,
            "get_db_connection",
            tracked_get_connection,
        )
        reconcile_task = asyncio.create_task(reconcile_stale_usage_reservations())
        await asyncio.wait_for(reconcile_write_attempted.wait(), timeout=1)
        assert not reconcile_task.done()
        await claim_conn.commit()

    assert await asyncio.wait_for(reconcile_task, timeout=1) == 0
    assert _fetchone(
        billing_db,
        "SELECT status FROM BILLING_USAGE_RESERVATIONS WHERE id = ?",
        (reservation_id,),
    )[0] == "active"
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_account_guard_is_reentrant_and_serializes_other_tasks():
    entered_nested = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with billing_account_guard(77):
            async def enter_from_keepalive_task():
                async with billing_account_guard(77):
                    entered_nested.set()

            await asyncio.wait_for(
                asyncio.create_task(enter_from_keepalive_task()),
                timeout=1,
            )
            await release_first.wait()

    async def second():
        async with billing_account_guard(77):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await asyncio.wait_for(entered_nested.wait(), timeout=1)
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_response_guard_is_held_until_stream_finishes(billing_db):
    first_chunk_started = asyncio.Event()
    release_stream = asyncio.Event()
    second_preflight_started = asyncio.Event()

    async def first_body():
        first_chunk_started.set()
        yield b"one"
        await release_stream.wait()
        yield b"two"

    async def make_first_response():
        return StreamingResponse(first_body())

    async def make_second_response():
        second_preflight_started.set()
        return SimpleNamespace(body_iterator=None)

    first_response = await serialize_user_billing_response(
        1,
        make_first_response(),
    )
    second_task = asyncio.create_task(
        serialize_user_billing_response(1, make_second_response())
    )

    async def consume_first_response():
        return [chunk async for chunk in first_response.body_iterator]

    consumer_task = asyncio.create_task(consume_first_response())
    await asyncio.wait_for(first_chunk_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not second_preflight_started.is_set()

    release_stream.set()
    assert await asyncio.wait_for(consumer_task, timeout=1) == [b"one", b"two"]
    await asyncio.wait_for(second_task, timeout=1)
    assert second_preflight_started.is_set()


@pytest.mark.asyncio
async def test_response_guard_releases_if_headers_cannot_be_sent(billing_db):
    async def body():
        yield b"unused"

    async def make_streaming_response():
        return StreamingResponse(body())

    response = await serialize_user_billing_response(
        1,
        make_streaming_response(),
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def failing_send(message):
        if message["type"] == "http.response.start":
            raise RuntimeError("send failed")

    with pytest.raises(RuntimeError, match="send failed"):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            failing_send,
        )

    async def make_non_streaming_response():
        return SimpleNamespace(body_iterator=None)

    await asyncio.wait_for(
        serialize_user_billing_response(1, make_non_streaming_response()),
        timeout=1,
    )


@pytest.mark.asyncio
async def test_second_stream_revalidates_after_first_stream_actual_charge(billing_db):
    observed_second_balance = None

    async def first_body():
        reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
        async with usage_reservations.get_db_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            credit = await prepare_ai_reservation_settlement(
                conn,
                reservation_id=reservation_id,
                user_id=1,
            )
            await conn.execute(
                "UPDATE USER_DETAILS SET balance = balance - 0.6 WHERE user_id = 1"
            )
            await complete_ai_reservation_settlement(conn, credit)
            await conn.commit()
        yield b"first-complete"

    async def make_first_response():
        return StreamingResponse(first_body())

    async def make_second_response():
        nonlocal observed_second_balance
        availability = await usage_reservations.get_user_billing_availability(1)
        observed_second_balance = availability["available"]
        return SimpleNamespace(body_iterator=None)

    first_response = await serialize_user_billing_response(1, make_first_response())
    second_task = asyncio.create_task(
        serialize_user_billing_response(1, make_second_response())
    )
    await asyncio.sleep(0)
    assert observed_second_balance is None

    assert [chunk async for chunk in first_response.body_iterator] == [
        b"first-complete"
    ]
    await asyncio.wait_for(second_task, timeout=1)
    assert observed_second_balance == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_generic_billing_stream_finishes_after_consumer_disconnect(billing_db):
    allow_finish = asyncio.Event()
    provider_finished = asyncio.Event()

    async def provider_stream():
        reservation_id = await reserve_ai_usage(user_id=1, maximum_amount=0.8)
        yield b"visible"
        await allow_finish.wait()
        async with usage_reservations.get_db_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            credit = await prepare_ai_reservation_settlement(
                conn,
                reservation_id=reservation_id,
                user_id=1,
            )
            await conn.execute(
                "UPDATE USER_DETAILS SET balance = balance - 0.3 WHERE user_id = 1"
            )
            await complete_ai_reservation_settlement(conn, credit)
            await conn.commit()
        provider_finished.set()
        yield b"discarded-after-disconnect"

    stream = serialize_user_billing_stream(1, provider_stream())
    assert await stream.__anext__() == b"visible"
    await stream.aclose()

    allow_finish.set()
    await asyncio.wait_for(provider_finished.wait(), timeout=1)
    assert _fetchone(
        billing_db,
        "SELECT balance FROM USER_DETAILS WHERE user_id = 1",
    )[0] == pytest.approx(0.7)

    async with billing_account_guard(1):
        pass
