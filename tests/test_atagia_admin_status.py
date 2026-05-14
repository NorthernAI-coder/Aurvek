from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _seed_atagia_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE messages (id TEXT PRIMARY KEY);
            CREATE TABLE memory_objects (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE summary_views (id TEXT PRIMARY KEY);
            CREATE TABLE graph_entities (id TEXT PRIMARY KEY);
            CREATE TABLE graph_relationships (id TEXT PRIMARY KEY);
            CREATE TABLE pending_memory_confirmations (id TEXT PRIMARY KEY);
            CREATE TABLE worker_job_runs (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                source_message_ids_json TEXT,
                queued_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                error_class TEXT,
                error_message TEXT
            );
            INSERT INTO messages (id) VALUES ('aurvek:msg:1'), ('aurvek:msg:2');
            INSERT INTO memory_objects (id, status) VALUES
                ('mem_1', 'active'),
                ('mem_2', 'active'),
                ('mem_3', 'review_required'),
                ('mem_4', 'pending_user_confirmation');
            INSERT INTO summary_views (id) VALUES ('summary_1');
            INSERT INTO pending_memory_confirmations (id) VALUES ('pending_1');
            INSERT INTO worker_job_runs (
                job_id,
                job_type,
                status,
                source_message_ids_json,
                queued_at,
                started_at,
                error_class,
                error_message
            ) VALUES
                (
                    'job_running',
                    'extract_memory_candidates',
                    'running',
                    '["aurvek:msg:1"]',
                    '2026-05-05T10:00:00+00:00',
                    '2026-05-05T10:00:01+00:00',
                    NULL,
                    NULL
                ),
                (
                    'job_queued',
                    'project_contract',
                    'queued',
                    '["aurvek:msg:2"]',
                    '2026-05-05T10:01:00+00:00',
                    NULL,
                    NULL,
                    NULL
                ),
                (
                    'job_failed',
                    'extract_memory_candidates',
                    'failed',
                    '["aurvek:msg:3"]',
                    '2026-05-05T10:02:00+00:00',
                    NULL,
                    'ExampleError',
                    'example failure'
                );
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_atagia_admin_status_includes_local_processing_stats(mock_db, tmp_path):
    import atagia_config
    from atagia_admin_status import get_atagia_admin_status

    atagia_config.invalidate_atagia_config_cache()
    atagia_db = tmp_path / "atagia.db"
    _seed_atagia_db(atagia_db)

    async with mock_db() as conn:
        await conn.executemany(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            [
                ("atagia_enabled", "true"),
                ("atagia_transport", "local"),
                ("atagia_db_path", str(atagia_db)),
            ],
        )
        await conn.commit()

    status = await get_atagia_admin_status()
    atagia = status["atagia"]

    assert atagia["available"] is True
    assert atagia["source"] == "local_db"
    assert atagia["table_counts"]["messages"] == 2
    assert atagia["table_counts"]["memory_objects"] == 4
    assert atagia["table_counts"]["summary_views"] == 1
    assert atagia["processing"]["status"] == "running"
    assert atagia["processing"]["pending_jobs"] == 1
    assert atagia["processing"]["running_jobs"] == 1
    assert atagia["processing"]["failed_jobs"] == 1
    assert atagia["processing"]["pending_jobs_by_type"] == {"project_contract": 1}
    assert atagia["active_jobs"][0]["job_id"] == "job_running"
    assert status["sync"]["linked_messages"] == 0


@pytest.mark.asyncio
async def test_atagia_admin_status_gracefully_skips_http_system_stats(mock_db):
    import atagia_config
    from atagia_admin_status import get_atagia_admin_status

    atagia_config.invalidate_atagia_config_cache()
    async with mock_db() as conn:
        await conn.executemany(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            [
                ("atagia_enabled", "true"),
                ("atagia_transport", "http"),
                ("atagia_base_url", "http://127.0.0.1:8100"),
            ],
        )
        await conn.commit()

    status = await get_atagia_admin_status()

    assert status["atagia"]["available"] is False
    assert status["atagia"]["source"] == "http"
    assert "local SQLite" in status["atagia"]["reason"]
    assert status["sync"]["pending_messages"] == 0
