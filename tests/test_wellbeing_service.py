import aiosqlite
import pytest

from wellbeing_service import (
    ensure_wellbeing_schema,
    get_admin_events,
    get_admin_live_sessions,
    get_admin_overview,
    get_status,
    get_user_preferences,
    get_user_wellbeing_summary,
    get_active_pause,
    record_chat_turn,
    record_user_action,
    record_voice_transcript_activity,
    reset_user_session,
    update_user_preferences,
    update_wellbeing_config,
)


pytestmark = pytest.mark.asyncio


async def _fetch_one(mock_db, query, params=()):
    async with mock_db() as conn:
        cursor = await conn.execute(query, params)
        return await cursor.fetchone()


async def test_wellbeing_schema_seeds_default_config(mock_db):
    await ensure_wellbeing_schema()

    row = await _fetch_one(
        mock_db,
        "SELECT value FROM SYSTEM_CONFIG WHERE key = 'wellbeing_enabled'",
    )

    assert row is not None
    assert row[0] == "1"


async def test_schema_handles_non_empty_legacy_system_config_table(tmp_path):
    db_path = tmp_path / "legacy_system_config.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("CREATE TABLE SYSTEM_CONFIG (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("INSERT INTO SYSTEM_CONFIG (key, value) VALUES ('legacy_key', 'legacy_value')")
        await conn.commit()

        await ensure_wellbeing_schema(conn)
        await conn.commit()

        cursor = await conn.execute(
            "SELECT value, updated_at FROM SYSTEM_CONFIG WHERE key = 'legacy_key'"
        )
        row = await cursor.fetchone()

    assert row[0] == "legacy_value"
    assert row[1] is not None


async def test_record_chat_turn_opens_session_and_counts_words(mock_db):
    await ensure_wellbeing_schema()

    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="hello there user",
        assistant_message="assistant response here",
    )

    session = status["session"]
    assert session["user_messages_count"] == 1
    assert session["assistant_messages_count"] == 1
    assert session["user_word_count"] == 3
    assert session["assistant_word_count"] == 3
    assert session["conversation_count"] == 1
    assert session["current_severity"] == "normal"


async def test_idle_gap_closes_old_session_and_starts_new_one(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({"wellbeing_idle_gap_minutes": 5})
    await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )

    async with mock_db() as conn:
        await conn.execute(
            """
            UPDATE USER_ACTIVITY_SESSIONS
            SET last_activity_at = datetime('now', '-10 minutes')
            WHERE user_id = 1
            """
        )
        await conn.commit()

    await record_chat_turn(
        user_id=1,
        conversation_id=11,
        user_message="second",
        assistant_message="reply",
    )

    row = await _fetch_one(
        mock_db,
        """
        SELECT
            SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_count,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count
        FROM USER_ACTIVITY_SESSIONS
        WHERE user_id = 1
        """,
    )
    assert row[0] == 1
    assert row[1] == 1


async def test_soft_threshold_reminder_and_snooze(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({
        "wellbeing_soft_user_messages": 1,
        "wellbeing_cooldown_minutes": 30,
        "wellbeing_snooze_minutes": 10,
    })

    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )

    assert status["session"]["current_severity"] == "soft"
    assert status["reminder"]["should_show"] is True

    await record_user_action(
        user_id=1,
        action="reminder_shown",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="soft",
        threshold_key=status["reminder"]["threshold_key"],
        threshold_value=status["reminder"]["threshold_value"],
        observed_value=status["reminder"]["observed_value"],
    )
    cooldown_status = await get_status(1)
    assert cooldown_status["reminder"]["should_show"] is False
    assert cooldown_status["reminder"]["reason"] == "cooldown"

    await record_user_action(
        user_id=1,
        action="reminder_snoozed",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="soft",
        snooze_minutes=10,
    )
    snoozed_status = await get_status(1)
    assert snoozed_status["reminder"]["should_show"] is False
    assert snoozed_status["reminder"]["reason"] in {"snoozed", "cooldown"}


async def test_preferences_can_disable_intense_reminders(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({
        "wellbeing_soft_user_messages": 100,
        "wellbeing_intense_user_messages": 1,
        "wellbeing_strong_user_messages": 100,
    })
    await update_user_preferences(1, {
        "reminders_enabled": True,
        "intense_reminders_enabled": False,
        "preferred_soft_minutes": None,
    })

    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )

    assert status["session"]["current_severity"] == "intense"
    assert status["reminder"]["should_show"] is False
    assert status["reminder"]["reason"] == "intense_disabled_by_user"

    preferences = await get_user_preferences(1)
    assert preferences["intense_reminders_enabled"] is False


async def test_strict_strong_reminder_ignores_cooldown_and_user_intense_preference(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({
        "wellbeing_mode": "strict",
        "wellbeing_cooldown_minutes": 60,
        "wellbeing_soft_user_messages": 100,
        "wellbeing_intense_user_messages": 100,
        "wellbeing_strong_user_messages": 1,
    })
    await update_user_preferences(1, {
        "reminders_enabled": True,
        "intense_reminders_enabled": False,
        "preferred_soft_minutes": None,
    })
    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )
    await record_user_action(
        user_id=1,
        action="reminder_shown",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="strong",
    )

    refreshed = await get_status(1)

    assert refreshed["reminder"]["should_show"] is True
    assert refreshed["reminder"]["requires_pause"] is True
    assert refreshed["reminder"]["allow_snooze"] is False


async def test_reset_user_session_closes_current_session(mock_db):
    await ensure_wellbeing_schema()
    await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )

    status = await reset_user_session(1, conversation_id=10)

    assert status["session"] is None
    row = await _fetch_one(
        mock_db,
        "SELECT status FROM USER_ACTIVITY_SESSIONS WHERE user_id = 1 ORDER BY id DESC LIMIT 1",
    )
    assert row[0] == "closed"


async def test_admin_and_usage_summaries_return_recorded_metrics(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({"wellbeing_soft_user_messages": 1})
    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="hello user",
        assistant_message="hello assistant",
    )
    await record_user_action(
        user_id=1,
        action="reminder_shown",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity=status["session"]["current_severity"],
    )

    overview = await get_admin_overview()
    live_sessions = await get_admin_live_sessions()
    events = await get_admin_events(event_type="reminder_shown")
    summary = await get_user_wellbeing_summary(1, days=30)

    assert overview["active_sessions"] == 1
    assert overview["reminders_shown"] == 1
    assert live_sessions[0]["user_id"] == 1
    assert events["total"] == 1
    assert summary["user_messages"] == 1
    assert summary["reminders_shown"] == 1
    assert summary["active_session"]["id"] == status["session"]["id"]


async def test_strict_strong_sessions_require_completed_pause(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({
        "wellbeing_mode": "strict",
        "wellbeing_soft_user_messages": 100,
        "wellbeing_intense_user_messages": 100,
        "wellbeing_strong_user_messages": 1,
    })
    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )

    pause = await get_active_pause(1)
    assert pause["reason"] == "strict_pause_required"

    with pytest.raises(ValueError):
        await record_user_action(
            user_id=1,
            action="reminder_snoozed",
            session_id=status["session"]["id"],
            conversation_id=10,
            severity="strong",
        )

    with pytest.raises(ValueError):
        await reset_user_session(1, conversation_id=10)

    await record_user_action(
        user_id=1,
        action="pause_started",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="strong",
        pause_minutes=5,
    )
    pause = await get_active_pause(1)
    assert pause["reason"] == "pause_active"

    async with mock_db() as conn:
        cursor = await conn.execute(
            "SELECT pause_until FROM USER_ACTIVITY_SESSIONS WHERE user_id = 1"
        )
        row = await cursor.fetchone()
        pause_until = row[0]
        cursor = await conn.execute(
            "SELECT (julianday(?) - julianday('now')) * 24 * 60",
            (pause_until,),
        )
        minutes = (await cursor.fetchone())[0]
    assert minutes >= 4.9

    with pytest.raises(ValueError):
        await record_user_action(
            user_id=1,
            action="pause_completed",
            session_id=status["session"]["id"],
            conversation_id=10,
            severity="strong",
        )

    async with mock_db() as conn:
        await conn.execute(
            """
            UPDATE USER_ACTIVITY_SESSIONS
            SET pause_until = datetime('now', '-1 minute')
            WHERE user_id = 1
            """
        )
        await conn.commit()

    assert await get_active_pause(1) is None
    completed_status = await get_status(1)
    assert completed_status["reminder"]["should_show"] is False
    assert completed_status["reminder"].get("requires_pause") is not True
    await record_user_action(
        user_id=1,
        action="pause_completed",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="strong",
    )
    assert await get_active_pause(1) is None


async def test_optional_pause_before_strong_does_not_satisfy_strict_pause(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({
        "wellbeing_mode": "strict",
        "wellbeing_soft_user_messages": 1,
        "wellbeing_intense_user_messages": 100,
        "wellbeing_strong_user_messages": 2,
    })
    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )
    assert status["session"]["current_severity"] == "soft"

    await record_user_action(
        user_id=1,
        action="pause_started",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="soft",
        pause_minutes=5,
    )
    async with mock_db() as conn:
        await conn.execute(
            """
            UPDATE USER_ACTIVITY_SESSIONS
            SET pause_until = datetime('now', '-1 minute')
            WHERE user_id = 1
            """
        )
        await conn.commit()

    status = await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="second",
        assistant_message="reply",
    )
    pause = await get_active_pause(1)

    assert status["session"]["current_severity"] == "strong"
    assert pause["reason"] == "strict_pause_required"
    assert status["reminder"]["requires_pause"] is True

    await record_user_action(
        user_id=1,
        action="pause_started",
        session_id=status["session"]["id"],
        conversation_id=10,
        severity="strong",
        pause_minutes=5,
    )
    async with mock_db() as conn:
        cursor = await conn.execute(
            "SELECT pause_reason FROM USER_ACTIVITY_SESSIONS WHERE user_id = 1"
        )
        row = await cursor.fetchone()
        await conn.execute(
            """
            UPDATE USER_ACTIVITY_SESSIONS
            SET pause_until = datetime('now', '-1 minute')
            WHERE user_id = 1
            """
        )
        await conn.commit()

    assert row[0] == "strict_strong"
    assert await get_active_pause(1) is None


async def test_stale_sessions_are_closed_on_status_and_admin_reads(mock_db):
    await ensure_wellbeing_schema()
    await update_wellbeing_config({"wellbeing_idle_gap_minutes": 5})
    await record_chat_turn(
        user_id=1,
        conversation_id=10,
        user_message="first",
        assistant_message="reply",
    )
    async with mock_db() as conn:
        await conn.execute(
            """
            UPDATE USER_ACTIVITY_SESSIONS
            SET started_at = datetime('now', '-2 days'),
                last_activity_at = datetime('now', '-2 days')
            WHERE user_id = 1
            """
        )
        await conn.commit()

    status = await get_status(1)
    overview = await get_admin_overview()
    live_sessions = await get_admin_live_sessions()

    assert status["session"] is None
    assert overview["active_sessions"] == 0
    assert live_sessions == []


async def test_voice_transcript_uses_elevenlabs_user_role_mapping(mock_db):
    await ensure_wellbeing_schema()

    status = await record_voice_transcript_activity(
        user_id=1,
        conversation_id=10,
        session_id="voice-session",
        transcript=[
            {"role": "customer", "message": "hello from caller"},
            {"role": "agent", "message": "hello from assistant"},
        ],
    )

    session = status["session"]
    assert session["user_messages_count"] == 1
    assert session["assistant_messages_count"] == 1
    assert session["user_word_count"] == 3
    assert session["assistant_word_count"] == 3


async def test_voice_transcript_does_not_create_empty_side_message(mock_db):
    await ensure_wellbeing_schema()

    status = await record_voice_transcript_activity(
        user_id=1,
        conversation_id=10,
        session_id="voice-session",
        transcript=[{"role": "customer", "message": "hello"}],
    )

    session = status["session"]
    assert session["user_messages_count"] == 1
    assert session["assistant_messages_count"] == 0
    assert session["user_word_count"] == 1
    assert session["assistant_word_count"] == 0
