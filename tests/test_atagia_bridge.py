from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

import atagia_bridge
from atagia_bridge import AtagiaBridge, AtagiaBridgeConfig

pytest.importorskip("atagia.integrations")


class FakeAtagiaClient:
    def __init__(self) -> None:
        self.created_users: list[str] = []
        self.created_conversations: list[dict[str, Any]] = []
        self.context_calls: list[dict[str, Any]] = []
        self.ingest_calls: list[dict[str, Any]] = []
        self.response_calls: list[dict[str, Any]] = []
        self.flush_calls: list[dict[str, Any]] = []
        self.worker_control_calls: list[dict[str, Any]] = []
        self.closed = False
        self.fail_context = False
        self.fail_ingest = False
        self.fail_response = False
        self.fail_worker_control = False
        self.context_result = object()
        self.worker_control_state: dict[str, Any] = {
            "mode": "active",
            "reason": None,
            "updated_at": "2026-05-05T12:00:00+00:00",
            "updated_by": "test",
            "new_source_jobs_allowed": True,
            "worker_claims_allowed": True,
            "periodic_work_allowed": True,
            "drain_completed": None,
        }

    async def create_user(self, user_id: str) -> None:
        self.created_users.append(user_id)

    async def create_conversation(
        self,
        user_id: str,
        conversation_id: str | None,
        *,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        mode: str | None = None,
        incognito: bool | None = None,
    ) -> str:
        self.created_conversations.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "user_persona_id": user_persona_id,
                "platform_id": platform_id,
                "character_id": character_id,
                "mode": mode,
                "incognito": incognito,
            }
        )
        return conversation_id or "generated_conversation"

    async def get_context(
        self,
        user_id: str,
        conversation_id: str,
        message: str,
        mode: str | None = None,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        message_id: str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        *,
        operational_profile: str | None = None,
        operational_signals: dict[str, Any] | None = None,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        incognito: bool | None = None,
    ) -> object:
        if self.fail_context:
            raise RuntimeError("context failure")
        self.context_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "message": message,
                "mode": mode,
                "occurred_at": occurred_at,
                "attachments": attachments,
                "message_id": message_id,
                "source_seq": source_seq,
                "ingest_origin": ingest_origin,
                "confirmation_strategy": confirmation_strategy,
                "memory_privacy_mode": memory_privacy_mode,
                "operational_profile": operational_profile,
                "operational_signals": operational_signals,
                "user_persona_id": user_persona_id,
                "platform_id": platform_id,
                "character_id": character_id,
                "incognito": incognito,
            }
        )
        return self.context_result

    async def ingest_message(
        self,
        user_id: str,
        conversation_id: str,
        role: str,
        text: str,
        mode: str | None = None,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        message_id: str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        *,
        operational_profile: str | None = None,
        operational_signals: dict[str, Any] | None = None,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        incognito: bool | None = None,
    ) -> bool:
        if self.fail_ingest:
            raise RuntimeError("ingest failure")
        self.ingest_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "role": role,
                "text": text,
                "mode": mode,
                "occurred_at": occurred_at,
                "attachments": attachments,
                "message_id": message_id,
                "source_seq": source_seq,
                "ingest_origin": ingest_origin,
                "confirmation_strategy": confirmation_strategy,
                "memory_privacy_mode": memory_privacy_mode,
                "operational_profile": operational_profile,
                "operational_signals": operational_signals,
                "user_persona_id": user_persona_id,
                "platform_id": platform_id,
                "character_id": character_id,
                "incognito": incognito,
            }
        )
        return True

    async def add_response(
        self,
        user_id: str,
        conversation_id: str,
        text: str,
        occurred_at: str | None = None,
        *,
        message_id: str | None = None,
        source_seq: int | str | None = None,
        ingest_origin: str | None = None,
        confirmation_strategy: str | None = None,
        memory_privacy_mode: str | None = None,
        operational_profile: str | None = None,
        operational_signals: dict[str, Any] | None = None,
        user_persona_id: str | None = None,
        platform_id: str | None = None,
        character_id: str | None = None,
        mode: str | None = None,
        incognito: bool | None = None,
    ) -> None:
        if self.fail_response:
            raise RuntimeError("response failure")
        self.response_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "text": text,
                "occurred_at": occurred_at,
                "message_id": message_id,
                "source_seq": source_seq,
                "ingest_origin": ingest_origin,
                "confirmation_strategy": confirmation_strategy,
                "memory_privacy_mode": memory_privacy_mode,
                "operational_profile": operational_profile,
                "operational_signals": operational_signals,
                "user_persona_id": user_persona_id,
                "platform_id": platform_id,
                "character_id": character_id,
                "mode": mode,
                "incognito": incognito,
            }
        )

    async def close(self) -> None:
        self.closed = True

    async def flush(self, timeout_seconds: float = 30.0, user_id: str | None = None) -> bool:
        self.flush_calls.append(
            {
                "timeout_seconds": timeout_seconds,
                "user_id": user_id,
            }
        )
        return True

    async def get_worker_control(self) -> dict[str, Any]:
        if self.fail_worker_control:
            raise RuntimeError("worker control failure")
        self.worker_control_calls.append({"operation": "get"})
        return dict(self.worker_control_state)

    async def set_worker_control(
        self,
        mode: Any,
        *,
        reason: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        if self.fail_worker_control:
            raise RuntimeError("worker control failure")
        resolved_mode = getattr(mode, "value", mode)
        self.worker_control_calls.append(
            {
                "operation": "set",
                "mode": resolved_mode,
                "reason": reason,
                "timeout_seconds": timeout_seconds,
            }
        )
        self.worker_control_state = {
            **self.worker_control_state,
            "mode": resolved_mode,
            "reason": reason,
            "new_source_jobs_allowed": resolved_mode == "active",
            "worker_claims_allowed": resolved_mode != "hard_pause",
            "periodic_work_allowed": resolved_mode == "active",
            "drain_completed": True if resolved_mode == "drain_and_pause" else None,
        }
        return dict(self.worker_control_state)


class FakeFactory:
    def __init__(self, client: FakeAtagiaClient | None = None) -> None:
        self.client = client or FakeAtagiaClient()
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> FakeAtagiaClient:
        self.calls.append(kwargs)
        return self.client


def test_config_from_env_parses_atagia_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATAGIA_ENABLED", "true")
    monkeypatch.setenv("ATAGIA_TRANSPORT", "http")
    monkeypatch.setenv("ATAGIA_DB_PATH", "/tmp/atagia.db")
    monkeypatch.setenv("ATAGIA_BASE_URL", "http://localhost:8100")
    monkeypatch.setenv("ATAGIA_SERVICE_API_KEY", "service-key")
    monkeypatch.setenv("ATAGIA_ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("ATAGIA_ASSISTANT_MODE", "companion")
    monkeypatch.setenv("ATAGIA_PLATFORM_ID", "aurvek-test")
    monkeypatch.setenv("ATAGIA_CHARACTER_ID", "global-character")
    monkeypatch.setenv("ATAGIA_USER_PERSONA_ID", "persona-1")
    monkeypatch.setenv("ATAGIA_INCOGNITO", "true")
    monkeypatch.setenv("ATAGIA_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("ATAGIA_MEMORY_PRIVACY_MODE", "trusted_private")

    config = AtagiaBridgeConfig.from_env()

    assert config.enabled is True
    assert config.transport == "http"
    assert config.db_path == "/tmp/atagia.db"
    assert config.base_url == "http://localhost:8100"
    assert config.api_key == "service-key"
    assert config.admin_api_key == "admin-key"
    assert config.assistant_mode == "companion"
    assert config.platform_id == "aurvek-test"
    assert config.character_id == "global-character"
    assert config.user_persona_id == "persona-1"
    assert config.incognito is False
    assert config.timeout_seconds == 12.5
    assert config.memory_privacy_mode == "trusted_private"


@pytest.mark.asyncio
async def test_disabled_bridge_does_not_initialize_client() -> None:
    async def fail_factory(**_kwargs: Any) -> FakeAtagiaClient:
        raise AssertionError("factory should not be called")

    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=False),
        client_factory=fail_factory,
    )

    assert await bridge.ensure_user_and_conversation(129, 1892) is None
    assert await bridge.get_context_for_turn(129, 1892, "hello") is None
    assert await bridge.record_assistant_response(129, 1892, "response") is False


@pytest.mark.asyncio
@respx.mock
async def test_http_purge_conversation_treats_404_as_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(atagia_bridge, "_atagia_path_segment", lambda value: value)
    respx.post("http://127.0.0.1:8100/v1/conversations/aurvek:conv:missing/close").mock(
        return_value=httpx.Response(404)
    )

    await atagia_bridge._http_purge_conversation(
        AtagiaBridgeConfig(
            enabled=True,
            transport="http",
            base_url="http://127.0.0.1:8100",
            api_key="service-key",
        ),
        user_id="aurvek:user:7",
        conversation_id="aurvek:conv:missing",
        character_id=None,
        incognito=False,
    )


@pytest.mark.asyncio
async def test_bridge_maps_aurvek_ids_and_mode_to_atagia_client() -> None:
    factory = FakeFactory()
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(
            enabled=True,
            transport="local",
            db_path="/tmp/atagia.db",
            assistant_mode="personal_assistant",
            platform_id="aurvek",
            user_persona_id="persona-1",
            timeout_seconds=7.0,
        ),
        client_factory=factory,
    )

    conversation_id = await bridge.ensure_user_and_conversation(129, 1892, prompt_id=42)
    context = await bridge.get_context_for_turn(
        129,
        1892,
        "remember this",
        occurred_at="2026-04-16T04:00:00+00:00",
        prompt_id=42,
        message_id=554,
        source_seq=554,
        ingest_origin="live_turn",
        confirmation_strategy="live_prompt_allowed",
        memory_privacy_mode="trusted_private",
    )
    recorded = await bridge.record_assistant_response(
        129,
        1892,
        "got it",
        prompt_id=42,
        message_id=555,
        source_seq=555,
        ingest_origin="live_turn",
        confirmation_strategy="live_prompt_allowed",
        memory_privacy_mode="trusted_private",
    )
    ingested = await bridge.ingest_message(
        129,
        1892,
        "user",
        "historical hello",
        occurred_at="2026-04-16T04:01:00+00:00",
        prompt_id=42,
        message_id=556,
        source_seq=556,
        ingest_origin="backfill",
        confirmation_strategy="admin_review_only",
        memory_privacy_mode="trusted_private",
    )
    flushed = await bridge.flush(user_id=129)

    assert conversation_id == "aurvek:conv:1892"
    assert context is factory.client.context_result
    assert recorded is True
    assert ingested is True
    assert flushed is True
    assert factory.calls == [
        {
            "transport": "local",
            "db_path": "/tmp/atagia.db",
            "base_url": None,
            "api_key": None,
            "timeout": 7.0,
        }
    ]
    assert factory.client.created_users == ["aurvek:user:129"]
    assert factory.client.created_conversations == [
        {
            "user_id": "aurvek:user:129",
            "conversation_id": "aurvek:conv:1892",
            "user_persona_id": "persona-1",
            "platform_id": "aurvek",
            "character_id": "prompt:42",
            "mode": "personal_assistant",
            "incognito": False,
        }
    ]
    assert factory.client.context_calls == [
        {
            "user_id": "aurvek:user:129",
            "conversation_id": "aurvek:conv:1892",
            "message": "remember this",
            "mode": "personal_assistant",
            "occurred_at": "2026-04-16T04:00:00+00:00",
            "attachments": None,
            "message_id": "aurvek:msg:554",
            "source_seq": 554,
            "ingest_origin": "live_turn",
            "confirmation_strategy": "live_prompt_allowed",
            "memory_privacy_mode": "trusted_private",
            "operational_profile": None,
            "operational_signals": {},
            "user_persona_id": "persona-1",
            "platform_id": "aurvek",
            "character_id": "prompt:42",
            "incognito": False,
        }
    ]
    assert factory.client.response_calls == [
        {
            "user_id": "aurvek:user:129",
            "conversation_id": "aurvek:conv:1892",
            "text": "got it",
            "occurred_at": None,
            "message_id": "aurvek:msg:555",
            "source_seq": 555,
            "ingest_origin": "live_turn",
            "confirmation_strategy": "live_prompt_allowed",
            "memory_privacy_mode": "trusted_private",
            "operational_profile": None,
            "operational_signals": {},
            "user_persona_id": "persona-1",
            "platform_id": "aurvek",
            "character_id": "prompt:42",
            "mode": "personal_assistant",
            "incognito": False,
        }
    ]
    assert factory.client.ingest_calls == [
        {
            "user_id": "aurvek:user:129",
            "conversation_id": "aurvek:conv:1892",
            "role": "user",
            "text": "historical hello",
            "mode": "personal_assistant",
            "occurred_at": "2026-04-16T04:01:00+00:00",
            "attachments": None,
            "message_id": "aurvek:msg:556",
            "source_seq": 556,
            "ingest_origin": "backfill",
            "confirmation_strategy": "admin_review_only",
            "memory_privacy_mode": "trusted_private",
            "operational_profile": None,
            "operational_signals": {},
            "user_persona_id": "persona-1",
            "platform_id": "aurvek",
            "character_id": "prompt:42",
            "incognito": False,
        }
    ]
    assert factory.client.flush_calls == [
        {
            "timeout_seconds": 7.0,
            "user_id": "aurvek:user:129",
        }
    ]


@pytest.mark.asyncio
async def test_bridge_passes_admin_api_key_to_http_sidecar_client() -> None:
    factory = FakeFactory()
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(
            enabled=True,
            transport="http",
            base_url="http://127.0.0.1:8100",
            api_key="service-key",
            admin_api_key="admin-key",
        ),
        client_factory=factory,
    )

    state = await bridge.get_worker_control()

    assert state["mode"] == "active"
    assert factory.calls == [
        {
            "transport": "http",
            "db_path": None,
            "base_url": "http://127.0.0.1:8100",
            "api_key": "service-key",
            "timeout": 30.0,
            "admin_api_key": "admin-key",
        }
    ]


@pytest.mark.asyncio
async def test_bridge_worker_control_actions_round_trip() -> None:
    factory = FakeFactory()
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=True, timeout_seconds=22.0),
        client_factory=factory,
    )

    paused = await bridge.pause_new_jobs(reason="maintenance")
    drained = await bridge.drain_and_pause(reason="restore", timeout_seconds=3.0)
    hard_paused = await bridge.hard_pause(reason="emergency")
    resumed = await bridge.resume_processing(reason="done")

    assert paused["mode"] == "pause_new_jobs"
    assert drained["mode"] == "drain_and_pause"
    assert drained["drain_completed"] is True
    assert hard_paused["mode"] == "hard_pause"
    assert resumed["mode"] == "active"
    assert factory.client.worker_control_calls == [
        {
            "operation": "set",
            "mode": "pause_new_jobs",
            "reason": "maintenance",
            "timeout_seconds": 22.0,
        },
        {
            "operation": "set",
            "mode": "drain_and_pause",
            "reason": "restore",
            "timeout_seconds": 3.0,
        },
        {
            "operation": "set",
            "mode": "hard_pause",
            "reason": "emergency",
            "timeout_seconds": 22.0,
        },
        {
            "operation": "set",
            "mode": "active",
            "reason": "done",
            "timeout_seconds": 22.0,
        },
    ]


@pytest.mark.asyncio
async def test_bridge_worker_control_fails_open() -> None:
    client = FakeAtagiaClient()
    client.fail_worker_control = True
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=True),
        client_factory=FakeFactory(client),
    )

    assert await bridge.get_worker_control() is None
    assert bridge.last_error is not None
    assert getattr(bridge.last_error, "operation", "") == "get_worker_control"
    assert await bridge.hard_pause(reason="stop") is None
    assert bridge.last_error is not None
    assert getattr(bridge.last_error, "operation", "") == "set_worker_control"


@pytest.mark.asyncio
async def test_bridge_fails_open_when_atagia_context_or_response_fails() -> None:
    client = FakeAtagiaClient()
    client.fail_context = True
    client.fail_ingest = True
    client.fail_response = True
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=True),
        client_factory=FakeFactory(client),
    )

    assert await bridge.get_context_for_turn(129, 1892, "hello") is None
    assert bridge.last_error is not None
    assert getattr(bridge.last_error, "operation", "") == "get_context_for_turn"
    assert await bridge.ingest_message(129, 1892, "user", "hello") is False
    assert bridge.last_error is not None
    assert getattr(bridge.last_error, "operation", "") == "ingest_message"
    assert await bridge.record_assistant_response(129, 1892, "response") is False
    assert bridge.last_error is not None
    assert getattr(bridge.last_error, "operation", "") == "record_assistant_response"


@pytest.mark.asyncio
async def test_bridge_per_call_incognito_overrides_default() -> None:
    factory = FakeFactory()
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=True, incognito=False),
        client_factory=factory,
    )

    await bridge.ensure_user_and_conversation(129, 1892, incognito=True)
    await bridge.get_context_for_turn(129, 1892, "private turn", incognito=True)
    await bridge.record_assistant_response(129, 1892, "private response", incognito=True)
    await bridge.ingest_message(129, 1892, "user", "private history", incognito=True)

    assert factory.client.created_conversations[0]["incognito"] is True
    assert factory.client.context_calls[0]["incognito"] is True
    assert factory.client.response_calls[0]["incognito"] is True
    assert factory.client.ingest_calls[0]["incognito"] is True


@pytest.mark.asyncio
async def test_bridge_ignores_global_incognito_config() -> None:
    factory = FakeFactory()
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=True, incognito=True),
        client_factory=factory,
    )

    await bridge.ensure_user_and_conversation(129, 1892)
    await bridge.get_context_for_turn(129, 1892, "normal turn")
    await bridge.record_assistant_response(129, 1892, "normal response")
    await bridge.ingest_message(129, 1892, "user", "normal history")

    assert factory.client.created_conversations[0]["incognito"] is False
    assert factory.client.context_calls[0]["incognito"] is False
    assert factory.client.response_calls[0]["incognito"] is False
    assert factory.client.ingest_calls[0]["incognito"] is False


@pytest.mark.asyncio
async def test_bridge_memory_preferences_round_trip_with_local_atagia(tmp_path) -> None:
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(
            enabled=True,
            transport="local",
            db_path=str(tmp_path / "atagia.db"),
        ),
    )

    initial = await bridge.get_memory_preferences(129)
    updated = await bridge.set_memory_preferences(
        129,
        remember_across_chats=False,
        memory_privacy_mode="trusted_private",
    )
    reloaded = await bridge.get_memory_preferences(129)

    assert initial["available"] is True
    assert initial["remember_across_chats"] is True
    assert initial["memory_privacy_mode"] == "balanced"
    assert updated["available"] is True
    assert updated["remember_across_chats"] is False
    assert updated["memory_privacy_mode"] == "trusted_private"
    assert reloaded["remember_across_chats"] is False
    assert reloaded["memory_privacy_mode"] == "trusted_private"


@pytest.mark.asyncio
async def test_bridge_close_closes_initialized_client() -> None:
    factory = FakeFactory()
    bridge = AtagiaBridge(
        AtagiaBridgeConfig(enabled=True),
        client_factory=factory,
    )

    assert await bridge.get_context_for_turn(129, 1892, "hello") is factory.client.context_result

    await bridge.close()

    assert factory.client.closed is True
