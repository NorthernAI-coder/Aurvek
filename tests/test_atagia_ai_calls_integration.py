import orjson
import pytest


class FakeBridge:
    def __init__(self, context_result=None):
        self.context_result = context_result
        self.context_calls = []
        self.ensure_calls = []
        self.response_calls = []

    async def get_context_for_turn(
        self,
        user_id,
        conversation_id,
        message_text,
        *,
        occurred_at=None,
        prompt_id=None,
        message_id=None,
        source_seq=None,
        ingest_origin=None,
        confirmation_strategy=None,
        incognito=None,
    ):
        self.context_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "message_text": message_text,
                "occurred_at": occurred_at,
                "prompt_id": prompt_id,
                "message_id": message_id,
                "source_seq": source_seq,
                "ingest_origin": ingest_origin,
                "confirmation_strategy": confirmation_strategy,
                "incognito": incognito,
            }
        )
        return self.context_result

    async def ensure_user_and_conversation(
        self,
        user_id,
        conversation_id,
        *,
        prompt_id=None,
        incognito=None,
    ):
        self.ensure_calls.append((user_id, conversation_id, prompt_id, incognito))
        return str(conversation_id)

    async def record_assistant_response(
        self,
        user_id,
        conversation_id,
        response_text,
        *,
        occurred_at=None,
        prompt_id=None,
        message_id=None,
        source_seq=None,
        ingest_origin=None,
        confirmation_strategy=None,
        incognito=None,
    ):
        self.response_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "response_text": response_text,
                "occurred_at": occurred_at,
                "prompt_id": prompt_id,
                "message_id": message_id,
                "source_seq": source_seq,
                "ingest_origin": ingest_origin,
                "confirmation_strategy": confirmation_strategy,
                "incognito": incognito,
            }
        )
        return True


class DummyUser:
    id = 7


def test_message_text_for_atagia_summarizes_multimodal_without_binary_payloads():
    from ai_runtime.atagia.context import _message_text_for_atagia

    message = [
        {"type": "text", "text": "remember this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"}},
        {"type": "document_bytes", "filename": "brief.pdf", "data": "BASE64PDF"},
    ]

    text = _message_text_for_atagia(message)

    assert "remember this" in text
    assert "[Image attached]" in text
    assert "[Document attached: brief.pdf]" in text
    assert "AAAABBBB" not in text
    assert "BASE64PDF" not in text


@pytest.mark.asyncio
async def test_augment_prompt_fetches_atagia_context_and_appends_system_prompt(monkeypatch):
    from ai_runtime.atagia import context as atagia_context

    bridge = FakeBridge({"system_prompt": "Memory says: prefers short answers."})
    monkeypatch.setattr(atagia_context, "get_atagia_bridge", lambda: bridge)

    prompt = await atagia_context._augment_prompt_with_atagia_context(
        "Base system prompt",
        user_id=7,
        conversation_id=99,
        message=[{"type": "text", "text": "hello"}],
        occurred_at="2026-04-16T12:00:00+00:00",
    )

    assert bridge.context_calls == [
        {
            "user_id": 7,
            "conversation_id": 99,
            "message_text": "hello",
            "occurred_at": "2026-04-16T12:00:00+00:00",
            "prompt_id": None,
            "message_id": None,
            "source_seq": None,
            "ingest_origin": "live_turn",
            "confirmation_strategy": "live_prompt_allowed",
            "incognito": None,
        }
    ]
    assert "Base system prompt" in prompt
    assert "[ATAGIA MEMORY CONTEXT - INTERNAL]" in prompt
    assert "Memory says: prefers short answers." in prompt


@pytest.mark.asyncio
async def test_atagia_context_decision_marks_primary_context_and_suppresses_local_history(monkeypatch):
    from ai_runtime.atagia import context as atagia_context

    bridge = FakeBridge({"system_prompt": "Memory says: prefers short answers."})
    monkeypatch.setattr(atagia_context, "get_atagia_bridge", lambda: bridge)

    decision = await atagia_context._resolve_atagia_context(
        "Base system prompt",
        user_id=7,
        conversation_id=99,
        message=[{"type": "text", "text": "hello"}],
    )
    local_history = [{"message": "old local turn", "type": "user"}]

    assert decision.active is True
    assert decision.reason == "active"
    assert "Memory says: prefers short answers." in decision.full_prompt
    assert atagia_context._context_messages_for_provider(local_history, decision) == []


@pytest.mark.asyncio
async def test_atagia_context_decision_keeps_local_history_when_context_missing(monkeypatch):
    from ai_runtime.atagia import context as atagia_context

    bridge = FakeBridge(None)
    monkeypatch.setattr(atagia_context, "get_atagia_bridge", lambda: bridge)

    decision = await atagia_context._resolve_atagia_context(
        "Base system prompt",
        user_id=7,
        conversation_id=99,
        message=[{"type": "text", "text": "hello"}],
    )
    local_history = [{"message": "old local turn", "type": "user"}]

    assert decision.active is False
    assert decision.reason == "no_context"
    assert decision.full_prompt == "Base system prompt"
    assert atagia_context._context_messages_for_provider(local_history, decision) == local_history


@pytest.mark.asyncio
async def test_record_atagia_assistant_response_flattens_multi_ai_payload(monkeypatch):
    from ai_runtime.atagia import recording as atagia_recording

    bridge = FakeBridge()
    monkeypatch.setattr(atagia_recording, "get_atagia_bridge", lambda: bridge)
    combined_response = orjson.dumps(
        {
            "multi_ai": True,
            "responses": [
                {"model": "gpt-5", "content": "First answer"},
                {"model": "claude", "content": "Second answer"},
            ],
        }
    ).decode()

    recorded = await atagia_recording._record_atagia_assistant_response(
        user_id=7,
        conversation_id=99,
        content=combined_response,
    )

    assert recorded is True
    assert bridge.response_calls == [
        {
            "user_id": 7,
            "conversation_id": 99,
            "response_text": "[gpt-5]\nFirst answer\n\n[claude]\nSecond answer",
            "occurred_at": None,
            "prompt_id": None,
            "message_id": None,
            "source_seq": None,
            "ingest_origin": "live_turn",
            "confirmation_strategy": "live_prompt_allowed",
            "incognito": None,
        }
    ]


@pytest.mark.asyncio
async def test_warmup_snapshot_primes_atagia_conversation_without_draft_text(monkeypatch):
    from ai_runtime.context import warmup as runtime_warmup
    from chat.services.warmup import WarmupCacheKey

    warmup_calls = []

    async def fake_context_messages(conversation_id, start_date):
        return [{"message": "previous", "type": "user"}]

    async def fake_prompt_runtime(conversation_id, current_user, effective_prompt_id):
        return {"full_prompt": "runtime prompt"}

    async def fake_warmup_atagia(user_id, conversation_id, *, prompt_id=None, incognito=None):
        warmup_calls.append((user_id, conversation_id, prompt_id, incognito))
        return True

    monkeypatch.setattr(runtime_warmup, "_load_warmup_context_messages", fake_context_messages)
    monkeypatch.setattr(runtime_warmup, "_load_warmup_prompt_runtime_snapshot", fake_prompt_runtime)
    monkeypatch.setattr(runtime_warmup, "_warmup_atagia_sidecar", fake_warmup_atagia)

    key = WarmupCacheKey(
        user_id=7,
        conversation_id=99,
        llm_id=1,
        effective_prompt_id=2,
        active_extension_id=0,
        last_message_id=10,
        mode="single",
    )
    snapshot = await runtime_warmup._build_chat_warmup_snapshot(
        conversation_id=99,
        current_user=DummyUser(),
        state={
            "llm_id": 1,
            "effective_prompt_id": 2,
            "active_extension_id": 0,
            "last_message_id": 10,
            "machine": "GPT",
            "model": "gpt-5",
        },
        cache_key=key,
        activity={"activity": "typing", "draft_length": 20},
    )

    assert warmup_calls == [(7, 99, 2, False)]
    assert snapshot["context_messages"] == [{"message": "previous", "type": "user"}]
    assert snapshot["sidecars"] == {"atagia_ready": True}
