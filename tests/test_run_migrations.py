import os
import sqlite3
import stat

import run_migrations


def test_database_backups_are_valid_and_private(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    backup_dir = db_dir / "backups"
    db_dir.mkdir()
    source = db_dir / "Aurvek.db"

    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker VALUES ('ready')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(run_migrations, "DB_DIR", str(db_dir))
    monkeypatch.setattr(run_migrations, "BACKUP_DIR", str(backup_dir))
    run_migrations.backup_databases()

    backups = list(backup_dir.glob("Aurvek_premigration_*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    assert conn.execute("SELECT value FROM marker").fetchone() == ("ready",)
    conn.close()

    if os.name != "nt":
        assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
