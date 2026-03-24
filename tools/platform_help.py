# tools/platform_help.py

import os
import logging
from tools import register_tool

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'db', 'Aurvek.db')

# ---- FTS5 query builder (adapted from message_search.py) ----

# Short tokens that are meaningful for KB search (not filtered by length check)
FTS_SHORT_ALLOWLIST = {'ai', 'ui', 'qr', 'tts', 'stt', 'pdf', 'mp3'}

STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'can', 'this', 'that', 'these',
    'those', 'it', 'its', 'my', 'your', 'his', 'her', 'our', 'their',
    'what', 'which', 'who', 'whom', 'how', 'when', 'where', 'why',
    'not', 'no', 'nor', 'so', 'if', 'then', 'than', 'too', 'very',
    'just', 'about', 'also', 'como', 'que', 'por', 'para', 'una', 'uno',
    'los', 'las', 'del', 'con', 'sin', 'mas', 'pero', 'hay', 'ser',
    'esta', 'este', 'ese', 'esa',
})

def build_help_fts_query(raw_query: str) -> tuple:
    """Build safe FTS5 queries from user input.
    Returns: (and_query: str, or_query: str) -- AND for strict matching, OR for relaxed fallback.
    """
    import re
    if not raw_query or not raw_query.strip():
        return ("", "")

    # Extract quoted phrases
    phrases = re.findall(r'"([^"]+)"', raw_query)
    remaining = re.sub(r'"[^"]*"', '', raw_query)

    # Normalize characters that are valid in KB content but break FTS5 syntax:
    # !voice -> voice,  multi-ai -> multi ai,  settings/profile -> settings profile
    remaining = remaining.replace('!', '')
    remaining = remaining.replace('/', ' ')
    remaining = re.sub(r'(?<=\w)-(?=\w)', ' ', remaining)  # hyphen between words only

    # Strip FTS5 operators
    remaining = re.sub(r'[*^{}\(\)\[\]|:]', '', remaining)

    # Quote FTS5 reserved words that might appear in natural queries
    FTS_RESERVED = {'AND', 'OR', 'NOT', 'NEAR'}

    parts = [f'"{p}"' for p in phrases if p.strip()]
    words = []
    for w_raw in remaining.split():
        w = w_raw.strip()
        if not w or w.lower() in STOPWORDS:
            continue
        if len(w) <= 2 and w.lower() not in FTS_SHORT_ALLOWLIST:
            continue
        # Quote FTS5 reserved words to treat them as literals
        if w.upper() in FTS_RESERVED:
            w = f'"{w}"'
        words.append(w)
    parts.extend(words)

    and_query = ' '.join(parts)
    or_query = ' OR '.join(parts) if len(parts) > 1 else and_query
    return (and_query, or_query)


async def lookup_platform_help(conn, query: str, category: str = None, user_role: str = 'customer') -> tuple:
    """
    Search the platform knowledge base.
    Returns: (formatted_text: str, article_count: int, top_article_id: str | None)
    Receives the existing aiosqlite read-only connection from get_ai_response().
    user_role filters articles by required_role: admin sees all, user sees user + null, customer sees only null.
    """
    fts_and_query, fts_or_query = build_help_fts_query(query)
    if not fts_and_query:
        return ("No results found. The query was empty.", 0, None)

    try:
        # Build the base SQL (reused for AND, OR, LIKE across category passes)
        base_sql = """
            SELECT
                a.article_id,
                a.title,
                a.category,
                a.short_answer,
                a.tool_text,
                a.prerequisites,
                bm25(HELP_ARTICLES_FTS) AS rank
            FROM HELP_ARTICLES_FTS f
            JOIN HELP_ARTICLES a ON a.id = f.rowid
            WHERE HELP_ARTICLES_FTS MATCH :fts_query
              AND a.is_active = 1
              AND a.approval_status = 'approved'
              AND a.tool_visible = 1
              AND (a.required_role IS NULL OR :user_role = 'admin' OR a.required_role = :user_role)
        """

        # Precompute LIKE word from sanitized FTS tokens (not raw query).
        fts_words = fts_and_query.replace('"', '').split()
        like_word = max(fts_words, key=len) if fts_words else query.strip()
        like_word = like_word.replace('~', '~~').replace('%', '~%').replace('_', '~_')
        if not like_word:
            like_word = query.strip().replace('~', '~~').replace('%', '~%').replace('_', '~_')
        like_query = f"%{like_word}%"

        # Skip LIKE fallback if token is too short (high false positive risk)
        like_min_length = 3
        skip_like = len(like_word) < like_min_length

        # Search cascade: try with category first, then without.
        rows = None
        for search_category in ([category, None] if category else [None]):
            # Step 1: FTS AND query (strict -- all keywords must match)
            params = {"fts_query": fts_and_query, "user_role": user_role}
            sql = base_sql
            if search_category:
                sql += " AND a.category = :category"
                params["category"] = search_category
            sql += " ORDER BY rank ASC LIMIT 3"

            try:
                cursor = await conn.execute(sql, params)
                rows = await cursor.fetchall()
                if rows:
                    break

                # Step 2: FTS OR query (relaxed -- any keyword can match)
                if fts_or_query != fts_and_query:
                    or_params = {**params, "fts_query": fts_or_query}
                    cursor = await conn.execute(sql, or_params)
                    rows = await cursor.fetchall()
                    if rows:
                        break
            except Exception as e:
                # FTS query syntax error (e.g., unhandled special chars) -- skip to LIKE
                logger.debug(f"[lookup_platform_help] FTS query failed, falling back to LIKE: {e}")

            # Step 3: LIKE fallback (only if we have a meaningful token)
            if skip_like:
                continue

            fallback_sql = """
                SELECT article_id, title, category, short_answer, tool_text, prerequisites,
                    CASE
                        WHEN title LIKE :like_query ESCAPE '~' THEN 1
                        WHEN keywords LIKE :like_query ESCAPE '~' THEN 2
                        ELSE 3
                    END AS rank
                FROM HELP_ARTICLES
                WHERE is_active = 1 AND approval_status = 'approved' AND tool_visible = 1
                  AND (title LIKE :like_query ESCAPE '~' OR keywords LIKE :like_query ESCAPE '~' OR short_answer LIKE :like_query ESCAPE '~')
                  AND (required_role IS NULL OR :user_role = 'admin' OR required_role = :user_role)
            """
            fb_params = {"like_query": like_query, "user_role": user_role}
            if search_category:
                fallback_sql += " AND category = :category"
                fb_params["category"] = search_category
            fallback_sql += " ORDER BY rank ASC LIMIT 3"

            cursor = await conn.execute(fallback_sql, fb_params)
            rows = await cursor.fetchall()
            if rows:
                break

        if not rows:
            return (
                "No platform help articles found for this query. "
                "You do not have confirmed information about this platform feature. "
                "Tell the user you don't have specific guidance for this and suggest they "
                "contact support or check the platform's help resources.",
                0,
                None
            )

        # Return top result with full tool_text, additional results as short summaries.
        results = []
        for i, row in enumerate(rows):
            if i <= 1:
                article = f"### {row['title']} (category: {row['category']})\n{row['tool_text']}\n"
            else:
                article = f"### Related: {row['title']}\n{row['short_answer']}\n"
            results.append(article)

        top_id = rows[0]['article_id'] if rows else None
        formatted = (
            "PLATFORM HELP RESULTS (use this information to answer the user):\n\n"
            + "\n---\n".join(results)
        )
        return (formatted, len(rows), top_id)

    except Exception as e:
        logger.error(f"[lookup_platform_help] FTS5 query failed: {e}")
        return ("Platform help search is temporarily unavailable. Tell the user to try again later.", 0, None)


async def log_help_query(query: str, user_message: str, category: str, results_count: int,
                         top_article_id: str, prompt_id: int):
    """Log the query for KB gap analysis. Uses get_db_connection for proper PRAGMAs.
    Stores HMAC-SHA256 hashes keyed with PEPPER -- no PII persisted in DB."""
    import hashlib, hmac
    from common import PEPPER
    normalized = query.strip().lower()
    query_hash = hmac.new(PEPPER.encode(), normalized.encode(), hashlib.sha256).hexdigest()
    user_query_hash = None
    if user_message:
        try:
            user_norm = str(user_message).strip().lower()
            user_query_hash = hmac.new(PEPPER.encode(), user_norm.encode(), hashlib.sha256).hexdigest()
        except Exception:
            pass  # non-critical telemetry
    try:
        from database import get_db_connection
        async with get_db_connection() as conn:
            await conn.execute(
                """INSERT INTO HELP_QUERY_LOG
                   (query_hash, user_query_hash, category, results_count, top_article_id, prompt_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (query_hash, user_query_hash, category, results_count, top_article_id, prompt_id)
            )
            await conn.commit()
    except Exception as e:
        logger.warning(f"[log_help_query] Failed to log: {e}")


# ---- Tool registration ----

register_tool({
    "type": "function",
    "function": {
        "name": "lookup_platform_help",
        "description": (
            "Look up how to use Aurvek platform features. Use this tool when the user asks "
            "how to do something on the platform, whether a feature exists, how something works, "
            "or needs help with platform functionality. Do NOT guess -- always use this tool for "
            "platform-related questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "2-5 English keywords describing the platform feature the user is asking about. Do NOT write a full sentence -- extract the key terms. Example: 'whatsapp continue conversation' instead of 'how do I continue a conversation on WhatsApp'. Include relevant keywords from the user's language as well for better matching."
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter to narrow results.",
                    "enum": [
                        "whatsapp", "telegram", "voice", "search",
                        "chat", "settings", "media", "auth", "billing",
                        "limitations"
                    ]
                }
            },
            "required": ["query"],
            "additionalProperties": False
        }
    },
    "strict": False
})
