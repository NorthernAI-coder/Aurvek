"""
Ingest help articles from docs/user_help/*.md into HELP_ARTICLES + FTS5.
Run: python ingest_help_articles.py
     python ingest_help_articles.py --rebuild   (clear FTS + re-process all articles)
Idempotent: skips unchanged files (SHA256 hash check), updates changed ones.
Deactivates DB articles whose source .md file no longer exists.
"""
import os, re, hashlib, json, sqlite3, sys, yaml

DOCS_DIR = os.path.join(os.path.dirname(__file__), 'docs', 'user_help')
DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'Aurvek.db')


def parse_article(filepath: str) -> dict:
    """Parse a markdown article with YAML frontmatter."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Normalize line endings (Windows CRLF -> LF)
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # Extract frontmatter
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
    if not match:
        raise ValueError(f"No valid frontmatter in {filepath}")

    meta = yaml.safe_load(match.group(1))

    # Validate required_role is explicitly set
    raw_role = meta.get('required_role')
    if raw_role is None and meta.get('approval_status') == 'approved':
        raise ValueError(f"Missing required 'required_role' (public/user/admin) in approved article {filepath}")
    if raw_role is not None and raw_role not in ('public', 'user', 'admin'):
        raise ValueError(f"Invalid required_role '{raw_role}' in {filepath} (must be public/user/admin)")

    # Validate frontmatter field types
    if not isinstance(meta.get('id'), str) or not meta['id'].strip():
        raise ValueError(f"'id' must be a non-empty string in {filepath}")
    if not isinstance(meta.get('title'), str) or not meta['title'].strip():
        raise ValueError(f"'title' must be a non-empty string in {filepath}")
    if not isinstance(meta.get('category'), str):
        raise ValueError(f"'category' must be a string in {filepath}")
    valid_categories = ('whatsapp', 'telegram', 'voice', 'search', 'chat', 'settings', 'media', 'auth', 'billing', 'limitations')
    if meta['category'] not in valid_categories:
        raise ValueError(f"Invalid category '{meta['category']}' in {filepath} (must be one of: {', '.join(valid_categories)})")
    if not isinstance(meta.get('keywords', []), list):
        raise ValueError(f"'keywords' must be a list in {filepath}")
    if meta.get('prerequisites') is not None and not isinstance(meta['prerequisites'], list):
        raise ValueError(f"'prerequisites' must be a list (or omitted) in {filepath}")
    if not isinstance(meta.get('tool_visible', False), bool):
        raise ValueError(f"'tool_visible' must be true or false in {filepath}")
    valid_statuses = ('draft', 'review', 'approved')
    if meta.get('approval_status', 'draft') not in valid_statuses:
        raise ValueError(f"Invalid approval_status '{meta.get('approval_status')}' in {filepath} (must be one of: {', '.join(valid_statuses)})")
    if meta.get('last_reviewed') is not None:
        lr = str(meta['last_reviewed'])
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', lr):
            raise ValueError(f"'last_reviewed' must be YYYY-MM-DD format in {filepath}, got '{lr}'")

    body = match.group(2).strip()

    # Extract short_answer from "## Short answer" section
    short_match = re.search(r'##\s*Short answer\s*\n+(.*?)(?=\n##|\Z)', body, re.DOTALL)
    if not short_match:
        raise ValueError(f"Missing required '## Short answer' section in {filepath}")
    short_answer = short_match.group(1).strip()

    # Extract optional sections for tool_text
    steps_match = re.search(r'##\s*Steps\s*\n+(.*?)(?=\n##|\Z)', body, re.DOTALL)
    notes_match = re.search(r'##\s*Notes\s*\n+(.*?)(?=\n##|\Z)', body, re.DOTALL)

    # Build tool_text: clean concatenation of structured sections for the AI
    tool_parts = [short_answer]
    if steps_match:
        tool_parts.append(steps_match.group(1).strip())
    if notes_match:
        tool_parts.append(notes_match.group(1).strip())

    # Incorporate prerequisites into tool_text (clean text, not JSON)
    prereqs = meta.get('prerequisites', [])
    if prereqs and isinstance(prereqs, list):
        prereq_text = "Prerequisites: " + ", ".join(str(p) for p in prereqs)
        tool_parts.append(prereq_text)

    tool_text = '\n\n'.join(tool_parts)

    # Size limit for tool_text
    TOOL_TEXT_MAX_CHARS = 2000
    if len(tool_text) > TOOL_TEXT_MAX_CHARS:
        if meta.get('approval_status') == 'approved' and meta.get('tool_visible', False):
            raise ValueError(
                f"tool_text exceeds {TOOL_TEXT_MAX_CHARS} chars ({len(tool_text)}) in approved+tool_visible article {filepath}. "
                f"Shorten ## Short answer, ## Steps, or ## Notes sections."
            )
        else:
            print(f"  WARNING: tool_text is {len(tool_text)} chars (max recommended: {TOOL_TEXT_MAX_CHARS})")

    # Compute file hash
    source_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()

    return {
        'article_id': meta['id'],
        'title': meta['title'],
        'category': meta['category'],
        'keywords': json.dumps(meta.get('keywords', []), ensure_ascii=False),
        'prerequisites': json.dumps(meta.get('prerequisites', []), ensure_ascii=False),
        'short_answer': short_answer,
        'tool_text': tool_text,
        'body': body,
        'tool_visible': 1 if meta.get('tool_visible', False) else 0,
        'approval_status': meta.get('approval_status', 'draft'),
        'required_role': None if meta.get('required_role') == 'public' else meta.get('required_role'),
        'last_reviewed_at': meta.get('last_reviewed'),
        'source_hash': source_hash,
    }


def ingest(rebuild: bool = False):
    if not os.path.isdir(DOCS_DIR):
        print(f"ERROR: Directory not found: {DOCS_DIR}")
        if '--tolerant' not in sys.argv:
            sys.exit(1)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if rebuild:
        print("--rebuild: Re-processing all articles (FTS will be rebuilt at the end)...")

    md_files = sorted(f for f in os.listdir(DOCS_DIR) if f.endswith('.md'))
    print(f"Found {len(md_files)} article files")

    if not md_files:
        print("ERROR: No .md files found in docs/user_help/")
        if '--tolerant' not in sys.argv:
            sys.exit(1)
        return

    # Pre-scan: detect duplicate article_ids across files
    id_to_files = {}
    for filename in md_files:
        filepath = os.path.join(DOCS_DIR, filename)
        try:
            article = parse_article(filepath)
            aid = article['article_id']
            id_to_files.setdefault(aid, []).append(filename)
        except Exception:
            pass  # parse errors handled in main loop
    duplicates = {aid: files for aid, files in id_to_files.items() if len(files) > 1}
    if duplicates:
        print("ERROR: Duplicate article_ids detected -- aborting ingest:")
        for aid, files in duplicates.items():
            print(f"  article_id '{aid}' in: {', '.join(files)}")
        conn.close()
        if '--tolerant' not in sys.argv:
            sys.exit(1)
        return

    inserted, updated, skipped, parse_errors, deactivated = 0, 0, 0, 0, 0
    seen_article_ids = set()

    # Track all filenames that EXIST on disk (regardless of parse success)
    existing_file_ids = {os.path.splitext(f)[0] for f in md_files}

    for filename in md_files:
        filepath = os.path.join(DOCS_DIR, filename)
        try:
            article = parse_article(filepath)
        except Exception as e:
            print(f"  ERROR {filename}: {e}")
            parse_errors += 1
            continue

        # Validate filename matches article_id
        expected_id = os.path.splitext(filename)[0]
        if article['article_id'] != expected_id:
            print(f"  ERROR {filename}: article_id '{article['article_id']}' does not match filename '{expected_id}'")
            parse_errors += 1
            continue

        seen_article_ids.add(article['article_id'])

        # Check if article exists and if hash changed
        existing = conn.execute(
            "SELECT id, source_hash, is_active FROM HELP_ARTICLES WHERE article_id = ?",
            (article['article_id'],)
        ).fetchone()

        if not rebuild and existing and existing['source_hash'] == article['source_hash'] and existing['is_active'] == 1:
            print(f"  UNCHANGED {filename}")
            skipped += 1
            continue

        if existing:
            # Update
            conn.execute("""
                UPDATE HELP_ARTICLES SET
                    title=?, category=?, keywords=?, prerequisites=?,
                    short_answer=?, tool_text=?, body=?, tool_visible=?, approval_status=?,
                    required_role=?, last_reviewed_at=?, source_hash=?, is_active=1
                WHERE article_id=?
            """, (
                article['title'], article['category'],
                article['keywords'], article['prerequisites'],
                article['short_answer'], article['tool_text'], article['body'],
                article['tool_visible'], article['approval_status'],
                article['required_role'], article['last_reviewed_at'],
                article['source_hash'], article['article_id']
            ))
            print(f"  UPDATED {filename}")
            updated += 1
        else:
            # Insert
            conn.execute("""
                INSERT INTO HELP_ARTICLES
                    (article_id, title, category, keywords, prerequisites,
                     short_answer, tool_text, body, tool_visible, approval_status,
                     required_role, last_reviewed_at, source_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article['article_id'], article['title'],
                article['category'], article['keywords'], article['prerequisites'],
                article['short_answer'], article['tool_text'], article['body'],
                article['tool_visible'], article['approval_status'],
                article['required_role'], article['last_reviewed_at'],
                article['source_hash']
            ))
            print(f"  INSERTED {filename}")
            inserted += 1

    # Abort safety: if ALL files failed to parse and nothing was ingested, abort completely
    if parse_errors > 0 and len(seen_article_ids) == 0:
        print(f"ERROR: All {parse_errors} files failed to parse -- aborting without committing")
        conn.close()
        if '--tolerant' not in sys.argv:
            sys.exit(1)
        return

    # Skip deactivation if ANY parse errors occurred
    if parse_errors > 0:
        print(f"  SKIPPING deactivation ({parse_errors} parse errors present)")
    elif existing_file_ids:
        placeholders = ','.join('?' * len(existing_file_ids))
        cursor = conn.execute(
            f"UPDATE HELP_ARTICLES SET is_active = 0 WHERE article_id NOT IN ({placeholders}) AND is_active = 1",
            list(existing_file_ids)
        )
        deactivated = cursor.rowcount
        if deactivated:
            print(f"  DEACTIVATED {deactivated} orphaned articles")

    # For --rebuild: reconstruct the entire FTS index from current HELP_ARTICLES state
    if rebuild:
        conn.execute("DELETE FROM HELP_ARTICLES_FTS")
        cursor = conn.execute("""
            INSERT INTO HELP_ARTICLES_FTS(rowid, title, short_answer, body, keywords)
            SELECT id, title, short_answer, body, keywords FROM HELP_ARTICLES
            WHERE is_active = 1 AND approval_status = 'approved' AND tool_visible = 1
        """)
        print(f"  FTS index rebuilt ({cursor.rowcount} articles indexed)")

    print(f"\nDone: {inserted} inserted, {updated} updated, {skipped} skipped, {parse_errors} errors, {deactivated} deactivated")

    # Check for empty ingest (no articles successfully processed)
    tolerant = '--tolerant' in sys.argv
    if inserted + updated + skipped == 0 and not tolerant:
        print("ERROR: No articles were successfully ingested")
        conn.rollback()
        conn.close()
        sys.exit(1)

    # Transaction control
    if parse_errors > 0 and not tolerant:
        conn.rollback()
        conn.close()
        print(f"STRICT MODE: {parse_errors} parse errors -- rolled back all changes, exiting with code 1")
        sys.exit(1)
    else:
        conn.commit()
        conn.close()
        if parse_errors > 0:
            print(f"TOLERANT MODE: {parse_errors} parse errors (committed partial results)")


if __name__ == '__main__':
    rebuild = '--rebuild' in sys.argv
    ingest(rebuild=rebuild)
