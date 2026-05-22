"""Runtime persistence and refresh helpers for marketplace flags."""

from __future__ import annotations

import asyncio
import os

from log_config import logger
from database import get_db_connection
from marketplace.config import (
    MARKETPLACE_FLAG_DEFINITIONS,
    load_marketplace_config_values,
)


async def load_marketplace_config_from_db() -> None:
    """Load marketplace runtime flags from SYSTEM_CONFIG into the process cache."""
    try:
        async with get_db_connection(readonly=True) as conn:
            placeholders = ",".join("?" for _ in MARKETPLACE_FLAG_DEFINITIONS)
            cursor = await conn.execute(
                f"SELECT key, value FROM SYSTEM_CONFIG WHERE key IN ({placeholders})",
                tuple(flag.key for flag in MARKETPLACE_FLAG_DEFINITIONS),
            )
            rows = await cursor.fetchall()
            values = {str(row["key"]): row["value"] for row in rows}
    except Exception as exc:
        logger.error("Failed to load marketplace config from DB: %s", exc)
        return

    load_marketplace_config_values(values)


def marketplace_config_refresh_interval_seconds() -> float:
    try:
        interval = float(os.getenv("MARKETPLACE_CONFIG_REFRESH_SECONDS", "5"))
    except ValueError:
        return 5.0
    return max(1.0, interval)


async def refresh_marketplace_config_loop() -> None:
    interval = marketplace_config_refresh_interval_seconds()
    while True:
        await asyncio.sleep(interval)
        await load_marketplace_config_from_db()


async def system_config_columns(conn) -> set[str]:
    cursor = await conn.execute("PRAGMA table_info(SYSTEM_CONFIG)")
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


async def upsert_system_config_value(conn, columns: set[str], key: str, value: str, description: str) -> None:
    if "description" in columns and "updated_at" in columns:
        await conn.execute(
            """
            INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                description = excluded.description,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value, description),
        )
    elif "updated_at" in columns:
        await conn.execute(
            """
            INSERT INTO SYSTEM_CONFIG (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
    else:
        await conn.execute(
            """
            INSERT INTO SYSTEM_CONFIG (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
