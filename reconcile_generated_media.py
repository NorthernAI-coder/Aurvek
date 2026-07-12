#!/usr/bin/env python3
"""Reconcile the GENERATED_MEDIA_FILES ledger against the filesystem.

The generated-media ledger (GENERATED_MEDIA_FILES) records one row per
AI-generated file on disk (images, videos, PDF/MP3 exports, voice-call WAV
recordings) so per-user storage usage can be summed cheaply. Rows are written at
generation time; a crash between writing a file and inserting its ledger row --
or a manual file deletion on the server -- can leave the ledger out of sync with
what is actually on disk.

This tool diffs the ledger against the filesystem and can repair it. THE
FILESYSTEM IS THE SOURCE OF TRUTH; the ledger is derived data.

Uploads (the content-addressed blob store) need NO reconciliation: their usage
is computed live from FILE_BLOBS / FILE_ATTACHMENTS and cannot drift. This tool
covers ONLY the generated-media ledger.

It also serves as the one-time post-deploy backfill: the ledger starts empty, so
running with --fix once populates it from the existing on-disk media.

Modes
    (default)   Report only. Walks data/users/*/*/*/files/** and reports:
                  - files on disk with no ledger row (undercount),
                  - ledger rows whose file is gone (overcount),
                  - rows whose recorded size != the real file size,
                  - files whose conversation no longer exists (manual action),
                  - files in unrecognized subdirectories (ignored).
    --fix       Sync the ledger to the filesystem: insert missing rows, delete
                orphan rows, update mismatched sizes. Files whose conversation no
                longer exists in the DB are SKIPPED and reported for manual
                action -- never inserted, never deleted.

Path -> kind mapping (normative): the FIRST directory segment under the
conversation dir decides -- img/** -> image, video/** -> video, pdf/** -> pdf,
mp3/** -> mp3, wav/** -> wav. Legacy img/user/** uploads that predate the blob
store live on the user's real disk and are counted as images. Files in any other
subdirectory are reported but never inserted and never deleted.

Output is ASCII-only (Windows cp1252 console).

Usage:
    python reconcile_generated_media.py                       # report only
    python reconcile_generated_media.py --fix                 # repair the ledger
    python reconcile_generated_media.py --db PATH --data-root PATH [--fix]
"""

import argparse
import os
import sqlite3
import sys

# Make sibling modules importable no matter the current working directory.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# normalize_rel_path is the ONE shared normalizer: disk walk and DB rows must
# meet in the same canonical space (forward slashes, relative to data/users/),
# or the UNIQUE upsert and rel_path-matched deletes silently miss.
from storage_quota import normalize_rel_path


# First directory segment under a conversation dir -> ledger kind. Mirrors the
# CHECK constraint on GENERATED_MEDIA_FILES.kind. img/bot and img/user both map
# to "image" because the FIRST segment (img) decides.
KIND_BY_DIR = {
    "img": "image",
    "video": "video",
    "pdf": "pdf",
    "mp3": "mp3",
    "wav": "wav",
}

REQUIRED_LEDGER_COLUMNS = {
    "id", "user_id", "conversation_id", "kind", "rel_path", "size_bytes",
}

# Cap per-category listings so a badly drifted tree does not flood the console.
MAX_LIST = 100


def _human_bytes(num_bytes):
    """ASCII, binary-unit size string (1 KB = 1024 B), sign-aware."""
    sign = "-" if num_bytes < 0 else ""
    value = float(abs(num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "%s%d B" % (sign, int(value))
            return "%s%.1f %s" % (sign, value, unit)
        value /= 1024.0


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _ledger_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(GENERATED_MEDIA_FILES)").fetchall()}


def _resolve_conversation_id(prefix1, prefix2):
    """Rebuild a conversation id from its two on-disk directory segments.

    The write path splits the zero-padded id string 3 + rest: e.g. id 1 ->
    "0000001" -> "000" / "0001". prefix1 is always exactly 3 chars; prefix2 is
    the remainder (>= 4 chars, longer for ids over 9,999,999). Returns the int
    id, or None when the segments are not a valid conversation dir.
    """
    if (
        len(prefix1) == 3
        and prefix1.isdigit()
        and len(prefix2) >= 4
        and prefix2.isdigit()
    ):
        return int(prefix1 + prefix2)
    return None


def _iter_files_dirs(users_dir):
    """Yield every data/users/{h1}/{h2}/{hash}/files directory that exists.

    Only the files/ subtree is walked -- profile pictures, avatars and
    marketplace assets under the same user dir are explicitly out of scope.
    """
    if not os.path.isdir(users_dir):
        return
    for h1 in sorted(os.listdir(users_dir)):
        h1_path = os.path.join(users_dir, h1)
        if not os.path.isdir(h1_path):
            continue
        for h2 in sorted(os.listdir(h1_path)):
            h2_path = os.path.join(h1_path, h2)
            if not os.path.isdir(h2_path):
                continue
            for user_hash in sorted(os.listdir(h2_path)):
                files_dir = os.path.join(h2_path, user_hash, "files")
                if os.path.isdir(files_dir):
                    yield files_dir


def scan_disk(users_dir):
    """Walk the files/ subtree and classify every file.

    Returns a dict of lists keyed by category. Each disk file becomes exactly
    one of: a known-kind entry (attributable to a conversation), an
    unattributable anomaly (known kind but unparseable conversation dir), or an
    unknown-subdir file. Known-kind entries carry the canonical rel_path, size,
    kind and conversation id.
    """
    known = {}          # canonical rel_path -> dict(rel, size, kind, conv_id)
    unknown_files = []  # (rel, size) -- not under a recognized media subdir
    anomalies = []      # (rel, reason) -- known kind but bad conversation dir
    files_dir_count = 0
    total_files = 0

    for files_dir in _iter_files_dirs(users_dir):
        files_dir_count += 1
        for root, _dirs, filenames in os.walk(files_dir):
            for filename in filenames:
                abs_path = os.path.join(root, filename)
                total_files += 1
                try:
                    size = os.path.getsize(abs_path)
                except OSError as exc:
                    anomalies.append((abs_path, "cannot stat file: %s" % exc))
                    continue

                rel_users = normalize_rel_path(os.path.relpath(abs_path, users_dir))
                rel_files_parts = os.path.relpath(abs_path, files_dir).replace("\\", "/").split("/")

                # Need at least {c1}/{c2}/{kind_dir}/file for a recognized media
                # file. Fewer segments = file sitting loose in the conversation
                # dir (no kind subdir) -> unknown.
                if len(rel_files_parts) < 4:
                    unknown_files.append((rel_users, size))
                    continue

                kind_dir = rel_files_parts[2]
                kind = KIND_BY_DIR.get(kind_dir)
                if kind is None:
                    unknown_files.append((rel_users, size))
                    continue

                conv_id = _resolve_conversation_id(rel_files_parts[0], rel_files_parts[1])
                if conv_id is None:
                    anomalies.append((rel_users, "unparseable conversation dir"))
                    continue

                known[rel_users] = {
                    "rel": rel_users,
                    "size": size,
                    "kind": kind,
                    "conv_id": conv_id,
                }

    return {
        "known": known,
        "unknown_files": unknown_files,
        "anomalies": anomalies,
        "files_dir_count": files_dir_count,
        "total_files": total_files,
    }


def load_ledger(conn):
    """Load every ledger row into a dict keyed by canonical rel_path.

    Rows whose stored rel_path cannot be normalized are returned separately as
    anomalies (a data problem, reported and left for manual action).
    """
    rows = {}
    bad_rows = []  # (raw_rel_path, reason)
    cursor = conn.execute(
        "SELECT id, user_id, conversation_id, kind, rel_path, size_bytes FROM GENERATED_MEDIA_FILES"
    )
    for row_id, user_id, conversation_id, kind, rel_path, size_bytes in cursor.fetchall():
        try:
            canonical = normalize_rel_path(rel_path)
        except ValueError as exc:
            bad_rows.append((rel_path, str(exc)))
            continue
        rows[canonical] = {
            "id": row_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "kind": kind,
            "rel_path": canonical,
            "size_bytes": int(size_bytes),
        }
    return rows, bad_rows


def load_conversation_owners(conn):
    """Map conversation id -> existing owner user id (single batched query).

    Historical production data contains conversations whose user row was
    deleted before foreign keys were enforced. Excluding those rows here keeps
    a single legacy orphan from rolling back the entire ledger backfill.
    """
    return {
        int(cid): int(uid)
        for cid, uid in conn.execute(
            """
            SELECT c.id, c.user_id
            FROM CONVERSATIONS c
            JOIN USERS u ON u.id = c.user_id
            """
        ).fetchall()
    }


def diff(disk, ledger, conv_owners, users_dir):
    """Compute the drift between filesystem and ledger.

    Ledger-side pass finds orphan rows (file gone) and size mismatches (both
    require an existing ledger row). Disk-side pass finds missing rows and splits
    them into insertable (conversation exists) vs orphaned files (conversation
    gone). No file is handled by both passes.
    """
    orphan_rows = []      # (rel, ledger_size)
    size_mismatches = []  # (rel, ledger_size, actual_size)
    missing_insertable = []   # dict(rel, size, kind, conv_id, user_id)
    orphaned_files = []       # dict(rel, size, kind, conv_id) -- conversation gone

    # Ledger-side: orphan rows + size mismatches.
    for canonical, row in ledger.items():
        abs_path = os.path.join(users_dir, *canonical.split("/"))
        if not os.path.exists(abs_path):
            orphan_rows.append((canonical, row["size_bytes"]))
            continue
        actual = os.path.getsize(abs_path)
        if actual != row["size_bytes"]:
            size_mismatches.append((canonical, row["size_bytes"], actual))

    # Disk-side: missing rows (known-kind files not represented in the ledger).
    for canonical, entry in disk["known"].items():
        if canonical in ledger:
            continue  # present in ledger -> already handled above
        conv_id = entry["conv_id"]
        user_id = conv_owners.get(conv_id)
        if user_id is None:
            orphaned_files.append(entry)
        else:
            missing_insertable.append({
                "rel": canonical,
                "size": entry["size"],
                "kind": entry["kind"],
                "conv_id": conv_id,
                "user_id": user_id,
            })

    return {
        "orphan_rows": orphan_rows,
        "size_mismatches": size_mismatches,
        "missing_insertable": missing_insertable,
        "orphaned_files": orphaned_files,
    }


def _print_listing(title, entries, formatter):
    if not entries:
        return
    print("")
    print("  %s (%d):" % (title, len(entries)))
    shown = entries[:MAX_LIST]
    for entry in shown:
        print("    %s" % formatter(entry))
    remaining = len(entries) - len(shown)
    if remaining > 0:
        print("    ... and %d more" % remaining)


def print_report(disk, ledger, drift, bad_rows, do_fix):
    orphan_rows = drift["orphan_rows"]
    size_mismatches = drift["size_mismatches"]
    missing_insertable = drift["missing_insertable"]
    orphaned_files = drift["orphaned_files"]
    unknown_files = disk["unknown_files"]
    anomalies = list(disk["anomalies"]) + [(rel, "unnormalizable ledger rel_path: %s" % reason)
                                           for rel, reason in bad_rows]

    insertable_bytes = sum(item["size"] for item in missing_insertable)
    orphaned_file_bytes = sum(item["size"] for item in orphaned_files)
    orphan_row_bytes = sum(size for _rel, size in orphan_rows)
    mismatch_abs = sum(abs(actual - ledger_size) for _rel, ledger_size, actual in size_mismatches)
    mismatch_net = sum(actual - ledger_size for _rel, ledger_size, actual in size_mismatches)
    unknown_bytes = sum(size for _rel, size in unknown_files)
    total_abs_drift = insertable_bytes + orphan_row_bytes + mismatch_abs

    print("")
    print("[SCAN] %d user file tree(s), %d file(s) on disk, %d ledger row(s)."
          % (disk["files_dir_count"], disk["total_files"], len(ledger)))
    print("")
    print("--- Drift summary ---")
    print("  Missing rows (file on disk, no ledger row):  %d file(s), %s"
          % (len(missing_insertable), _human_bytes(insertable_bytes)))
    print("  Orphaned files (conversation gone, manual):  %d file(s), %s"
          % (len(orphaned_files), _human_bytes(orphaned_file_bytes)))
    print("  Orphan rows (ledger row, file gone):         %d row(s),  %s"
          % (len(orphan_rows), _human_bytes(orphan_row_bytes)))
    print("  Size mismatches (row size != file size):     %d row(s),  net %s / abs %s"
          % (len(size_mismatches), _human_bytes(mismatch_net), _human_bytes(mismatch_abs)))
    print("  Unknown-subdir files (ignored):              %d file(s), %s"
          % (len(unknown_files), _human_bytes(unknown_bytes)))
    print("  Anomalies (reported, never touched):         %d" % len(anomalies))
    print("")
    print("  Total absolute ledger drift (fixable):       %s" % _human_bytes(total_abs_drift))

    _print_listing(
        "Missing rows -> would insert", missing_insertable,
        lambda e: "[%s] %s  (%s, conv %d, user %d)"
                  % (e["kind"], e["rel"], _human_bytes(e["size"]), e["conv_id"], e["user_id"]),
    )
    _print_listing(
        "Orphaned files -> MANUAL action (conversation gone)", orphaned_files,
        lambda e: "[%s] %s  (%s, conv %d not in DB)"
                  % (e["kind"], e["rel"], _human_bytes(e["size"]), e["conv_id"]),
    )
    _print_listing(
        "Orphan rows -> would delete", orphan_rows,
        lambda e: "%s  (recorded %s, file gone)" % (e[0], _human_bytes(e[1])),
    )
    _print_listing(
        "Size mismatches -> would update", size_mismatches,
        lambda e: "%s  (row %s -> actual %s)" % (e[0], _human_bytes(e[1]), _human_bytes(e[2])),
    )
    _print_listing(
        "Unknown-subdir files -> ignored", unknown_files,
        lambda e: "%s  (%s)" % (e[0], _human_bytes(e[1])),
    )
    _print_listing(
        "Anomalies -> reported only", anomalies,
        lambda e: "%s  (%s)" % (e[0], e[1]),
    )

    print("")
    if do_fix:
        return  # apply_fix prints its own action summary
    if total_abs_drift == 0 and not orphaned_files and not anomalies:
        print("[OK] Ledger matches the filesystem. No changes needed.")
    else:
        print("[NOTE] Report only. Re-run with --fix to apply the changes above.")
        if orphaned_files:
            print("[WARN] Orphaned files need MANUAL review -- --fix will not touch them.")


def apply_fix(conn, drift):
    """Insert missing rows, delete orphan rows, update mismatched sizes.

    Orphaned files (conversation gone), unknown-subdir files and anomalies are
    intentionally left untouched. Everything commits in one transaction; any
    error rolls the whole thing back (fail fast).
    """
    missing_insertable = drift["missing_insertable"]
    orphan_rows = drift["orphan_rows"]
    size_mismatches = drift["size_mismatches"]

    inserted = 0
    deleted = 0
    updated = 0
    try:
        for item in missing_insertable:
            # Canonical upsert: a concurrent write that already inserted the row
            # collapses to a size update instead of a UNIQUE violation.
            conn.execute(
                """
                INSERT INTO GENERATED_MEDIA_FILES
                    (user_id, conversation_id, kind, rel_path, size_bytes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET size_bytes = excluded.size_bytes
                """,
                (item["user_id"], item["conv_id"], item["kind"], item["rel"], item["size"]),
            )
            inserted += 1

        for rel, _size in orphan_rows:
            cursor = conn.execute(
                "DELETE FROM GENERATED_MEDIA_FILES WHERE rel_path = ?", (rel,)
            )
            deleted += int(cursor.rowcount or 0)

        for rel, _ledger_size, actual in size_mismatches:
            cursor = conn.execute(
                "UPDATE GENERATED_MEDIA_FILES SET size_bytes = ? WHERE rel_path = ?",
                (actual, rel),
            )
            updated += int(cursor.rowcount or 0)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print("[FIX] Inserted %d row(s), deleted %d orphan row(s), updated %d size(s)."
          % (inserted, deleted, updated))
    skipped = len(drift["orphaned_files"])
    if skipped:
        print("[WARN] Skipped %d orphaned file(s) (conversation gone) -- manual action required."
              % skipped)
    print("[OK] Ledger reconciled with the filesystem.")


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile the GENERATED_MEDIA_FILES ledger against the filesystem.",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Apply changes (insert/delete/update). Default is report only.",
    )
    parser.add_argument(
        "--db", default=os.path.join(PROJECT_ROOT, "db", "Aurvek.db"),
        help="Path to the SQLite database (default: db/Aurvek.db).",
    )
    parser.add_argument(
        "--data-root", default=os.path.join(PROJECT_ROOT, "data"),
        help="Directory containing the users/ tree (default: data/). "
             "The users/ subdirectory is walked.",
    )
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    data_root = os.path.abspath(args.data_root)
    users_dir = os.path.join(data_root, "users")

    if not os.path.exists(db_path):
        print("[FAIL] Database not found: %s" % db_path)
        return 1

    mode = "FIX" if args.fix else "report only"
    print("[REPORT] Generated-media ledger reconcile (%s)" % mode)
    print("  DB:        %s" % db_path)
    print("  Users dir: %s" % users_dir)
    print("  NOTE: uploads (blob store) are computed live and need no reconciliation;")
    print("        this tool reconciles ONLY the generated-media ledger.")

    if not os.path.isdir(users_dir):
        print("[FAIL] Users directory does not exist: %s" % users_dir)
        print("       Refusing to reconcile: a wrong data root could invalidate the ledger.")
        return 1

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")

        # Fail fast on an unexpected schema rather than silently doing nothing.
        if not _table_exists(conn, "GENERATED_MEDIA_FILES"):
            print("[FAIL] Table GENERATED_MEDIA_FILES not found. "
                  "Run migration_storage_quotas.py first.")
            return 1
        if not _table_exists(conn, "CONVERSATIONS"):
            print("[FAIL] Table CONVERSATIONS not found. Database is not initialized.")
            return 1
        if not _table_exists(conn, "USERS"):
            print("[FAIL] Table USERS not found. Database is not initialized.")
            return 1
        missing_cols = REQUIRED_LEDGER_COLUMNS - _ledger_columns(conn)
        if missing_cols:
            print("[FAIL] GENERATED_MEDIA_FILES is missing columns: %s"
                  % ", ".join(sorted(missing_cols)))
            return 1

        ledger, bad_rows = load_ledger(conn)
        conv_owners = load_conversation_owners(conn)
        disk = scan_disk(users_dir)
        drift = diff(disk, ledger, conv_owners, users_dir)

        print_report(disk, ledger, drift, bad_rows, args.fix)

        if args.fix:
            apply_fix(conn, drift)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
