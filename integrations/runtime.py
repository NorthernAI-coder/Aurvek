from database import get_db_connection


async def ensure_integration_schema() -> None:
    """Create lightweight integration tables that predate formal migrations."""
    async with get_db_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS WHATSAPP_PROCESSED_MESSAGES (
                message_sid TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS WHATSAPP_LOG (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                phone_number TEXT,
                direction TEXT CHECK(direction IN ('in', 'out')),
                message_type TEXT CHECK(message_type IN ('text', 'audio', 'image', 'error', 'system')),
                response_mode TEXT CHECK(response_mode IN ('text', 'voice')),
                FOREIGN KEY (user_id) REFERENCES USERS(id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS TELEGRAM_PROCESSED_UPDATES (
                update_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS TELEGRAM_LOG (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                chat_id INTEGER,
                direction TEXT CHECK(direction IN ('in', 'out')),
                message_type TEXT CHECK(message_type IN ('text', 'audio', 'image', 'contact', 'error', 'system')),
                response_mode TEXT CHECK(response_mode IN ('text', 'voice'))
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_telegram_log_timestamp
            ON TELEGRAM_LOG(timestamp)
            """
        )
        await conn.commit()
