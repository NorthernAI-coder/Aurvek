"""Admin diagnostics for Aurvek's Atagia integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from atagia_config import get_atagia_bridge_config
from atagia_sync import get_atagia_sync_status
from log_config import logger


NONTERMINAL_JOB_STATUSES = {"queued", "running", "retrying"}
ERROR_JOB_STATUSES = {"failed", "dead_lettered"}
KNOWN_TABLES = {
    "messages",
    "memory_objects",
    "summary_views",
    "graph_entities",
    "graph_relationships",
    "pending_memory_confirmations",
    "worker_job_runs",
}


async def get_atagia_admin_status() -> dict[str, Any]:
    """Return admin-facing sync and Atagia processing diagnostics."""
    config = await get_atagia_bridge_config()
    sync_status = await get_atagia_sync_status()
    diagnostics = await _get_atagia_local_db_diagnostics(config)
    return {
        "sync": sync_status,
        "atagia": diagnostics,
    }


async def _get_atagia_local_db_diagnostics(config: Any) -> dict[str, Any]:
    if not config.enabled:
        return {
            "available": False,
            "source": "disabled",
            "reason": "Atagia is disabled.",
        }

    if not _uses_local_db(config):
        return {
            "available": False,
            "source": "http",
            "reason": "System-wide diagnostics are available for local SQLite transport.",
        }

    db_path = _resolve_db_path(config.db_path)
    if not db_path.exists():
        return {
            "available": False,
            "source": "local_db",
            "db_path": str(db_path),
            "reason": "Atagia SQLite database was not found.",
        }

    try:
        async with aiosqlite.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout=5000")
            tables = await _existing_tables(conn)
            table_counts = await _table_counts(conn, tables)
            memory_status_counts = await _group_counts(
                conn,
                tables,
                "memory_objects",
                "status",
            )
            job_status_counts = await _group_counts(
                conn,
                tables,
                "worker_job_runs",
                "status",
            )
            job_type_counts = await _job_type_counts(conn, tables)
            active_jobs = await _active_jobs(conn, tables)
    except Exception as exc:
        logger.warning("Failed to load Atagia admin diagnostics", exc_info=True)
        return {
            "available": False,
            "source": "local_db",
            "db_path": str(db_path),
            "reason": f"Could not read Atagia diagnostics: {exc}",
        }

    processing = _processing_summary(job_status_counts, job_type_counts)
    return {
        "available": True,
        "source": "local_db",
        "db_path": str(db_path),
        "table_counts": table_counts,
        "memory_status_counts": memory_status_counts,
        "job_status_counts": job_status_counts,
        "job_type_counts": job_type_counts,
        "active_jobs": active_jobs,
        "processing": processing,
    }


def _uses_local_db(config: Any) -> bool:
    transport = str(getattr(config, "transport", "auto") or "auto").lower()
    if transport == "local":
        return True
    if transport == "http":
        return False
    return not bool(getattr(config, "base_url", None))


def _resolve_db_path(value: str | None) -> Path:
    raw = (value or "db/atagia.db").strip() or "db/atagia.db"
    path = Path(raw)
    if path.is_absolute():
        return path
    return (Path(__file__).resolve().parent / path).resolve()


async def _existing_tables(conn: aiosqlite.Connection) -> set[str]:
    placeholders = ", ".join("?" for _ in KNOWN_TABLES)
    cursor = await conn.execute(
        f"""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ({placeholders})
        """,
        tuple(sorted(KNOWN_TABLES)),
    )
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


async def _table_counts(
    conn: aiosqlite.Connection,
    tables: set[str],
) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for table in sorted(KNOWN_TABLES):
        if table not in tables:
            counts[table] = None
            continue
        cursor = await conn.execute(f"SELECT COUNT(*) AS count_value FROM {table}")
        row = await cursor.fetchone()
        counts[table] = int(row["count_value"] if row else 0)
    return counts


async def _group_counts(
    conn: aiosqlite.Connection,
    tables: set[str],
    table: str,
    column: str,
) -> list[dict[str, Any]]:
    if table not in tables or not await _table_has_column(conn, table, column):
        return []
    cursor = await conn.execute(
        f"""
        SELECT {column} AS key_value, COUNT(*) AS count_value
        FROM {table}
        GROUP BY {column}
        ORDER BY count_value DESC, key_value ASC
        """
    )
    rows = await cursor.fetchall()
    return [
        {
            "key": str(row["key_value"] or "unknown"),
            "count": int(row["count_value"] or 0),
        }
        for row in rows
    ]


async def _job_type_counts(
    conn: aiosqlite.Connection,
    tables: set[str],
) -> list[dict[str, Any]]:
    if "worker_job_runs" not in tables:
        return []
    if not await _table_has_columns(conn, "worker_job_runs", {"job_type", "status"}):
        return []

    cursor = await conn.execute(
        """
        SELECT job_type, status, COUNT(*) AS count_value
        FROM worker_job_runs
        GROUP BY job_type, status
        ORDER BY job_type ASC, status ASC
        """
    )
    rows = await cursor.fetchall()
    by_type: dict[str, dict[str, Any]] = {}
    for row in rows:
        job_type = str(row["job_type"] or "unknown")
        status = str(row["status"] or "unknown")
        count = int(row["count_value"] or 0)
        entry = by_type.setdefault(job_type, {"job_type": job_type, "statuses": {}, "total": 0})
        entry["statuses"][status] = count
        entry["total"] += count

    return sorted(
        by_type.values(),
        key=lambda item: (
            -sum(item["statuses"].get(status, 0) for status in NONTERMINAL_JOB_STATUSES),
            str(item["job_type"]),
        ),
    )


async def _active_jobs(
    conn: aiosqlite.Connection,
    tables: set[str],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    if "worker_job_runs" not in tables:
        return []
    columns = await _table_columns(conn, "worker_job_runs")
    required = {"job_id", "job_type", "status", "queued_at"}
    if not required.issubset(columns):
        return []

    optional_columns = {
        "source_message_ids_json",
        "started_at",
        "finished_at",
        "error_class",
        "error_message",
    }
    select_columns = [
        "job_id",
        "job_type",
        "status",
        "queued_at",
        *(column for column in optional_columns if column in columns),
    ]
    placeholders = ", ".join("?" for _ in (NONTERMINAL_JOB_STATUSES | ERROR_JOB_STATUSES))
    cursor = await conn.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM worker_job_runs
        WHERE status IN ({placeholders})
        ORDER BY
            CASE status
                WHEN 'running' THEN 0
                WHEN 'retrying' THEN 1
                WHEN 'queued' THEN 2
                WHEN 'failed' THEN 3
                WHEN 'dead_lettered' THEN 4
                ELSE 5
            END,
            queued_at ASC
        LIMIT ?
        """,
        tuple(sorted(NONTERMINAL_JOB_STATUSES | ERROR_JOB_STATUSES)) + (limit,),
    )
    rows = await cursor.fetchall()
    return [_job_row_to_dict(row) for row in rows]


def _job_row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    values = {key: row[key] for key in row.keys()}
    source_messages = _parse_json_list(values.get("source_message_ids_json"))
    return {
        "job_id": str(values.get("job_id") or ""),
        "job_type": str(values.get("job_type") or "unknown"),
        "status": str(values.get("status") or "unknown"),
        "source_messages": source_messages[:3],
        "queued_at": values.get("queued_at"),
        "started_at": values.get("started_at"),
        "finished_at": values.get("finished_at"),
        "error_class": values.get("error_class"),
        "error_message": _truncate(values.get("error_message"), 180),
    }


def _parse_json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _processing_summary(
    job_status_counts: list[dict[str, Any]],
    job_type_counts: list[dict[str, Any]],
) -> dict[str, Any]:
    by_status = {str(item["key"]): int(item["count"]) for item in job_status_counts}
    pending_jobs = by_status.get("queued", 0)
    running_jobs = by_status.get("running", 0)
    retrying_jobs = by_status.get("retrying", 0)
    failed_jobs = by_status.get("failed", 0)
    dead_lettered_jobs = by_status.get("dead_lettered", 0)
    active_jobs = pending_jobs + running_jobs + retrying_jobs

    if retrying_jobs:
        status = "retrying"
    elif running_jobs:
        status = "running"
    elif pending_jobs:
        status = "queued"
    elif failed_jobs or dead_lettered_jobs:
        status = "degraded"
    else:
        status = "idle"

    if active_jobs == 0:
        queue_state = "idle"
    elif active_jobs <= 10:
        queue_state = "normal"
    elif active_jobs <= 100:
        queue_state = "busy"
    else:
        queue_state = "backlogged"

    return {
        "status": status,
        "processing": active_jobs > 0,
        "queue_state": queue_state,
        "pending_jobs": pending_jobs,
        "running_jobs": running_jobs,
        "retrying_jobs": retrying_jobs,
        "failed_jobs": failed_jobs,
        "dead_lettered_jobs": dead_lettered_jobs,
        "active_jobs": active_jobs,
        "pending_jobs_by_type": _status_by_type(job_type_counts, "queued"),
        "running_jobs_by_type": _status_by_type(job_type_counts, "running"),
    }


def _status_by_type(
    job_type_counts: list[dict[str, Any]],
    status: str,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in job_type_counts:
        count = int(item.get("statuses", {}).get(status, 0) or 0)
        if count:
            result[str(item.get("job_type") or "unknown")] = count
    return result


async def _table_has_columns(
    conn: aiosqlite.Connection,
    table: str,
    columns: set[str],
) -> bool:
    existing = await _table_columns(conn, table)
    return columns.issubset(existing)


async def _table_has_column(
    conn: aiosqlite.Connection,
    table: str,
    column: str,
) -> bool:
    return column in await _table_columns(conn, table)


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."
