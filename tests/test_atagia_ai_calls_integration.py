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
    ):
        self.context_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "message_text": message_text,
                "occurred_at": occurred_at,
            }
        )
        return self.context_result

    async def ensure_user_and_conversation(self, user_id, conversation_id):
        self.ensure_calls.append((user_id, conversation_id))
        return str(conversation_id)

    async def record_assistant_response(
        self,
        user_id,
        conversation_id,
        response_text,
        *,
        occurred_at=None,
    ):
        self.response_calls.append(
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "response_text": response_text,
                "occurred_at": occurred_at,
            }
        )
        return True


class DummyUser:
    id = 7


def test_message_text_for_atagia_summarizes_multimodal_without_binary_payloads():
    import ai_calls

    message = [
        {"type": "text", "text": "remember this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"}},
        {"type": "document_bytes", "filename": "brief.pdf", "data": "BASE64PDF"},
    ]

    text = ai_calls._message_text_for_atagia(message)

    assert "remember this" in text
    assert "[Image attached]" in text
    assert "[Document attached: brief.pdf]" in text
    assert "AAAABBBB" not in text
    assert "BASE64PDF" not in text


@pytest.mark.asyncio
async def test_augment_prompt_fetches_atagia_context_and_appends_system_prompt(monkeypatch):
    import ai_calls

    bridge = FakeBridge({"system_prompt": "Memory says: prefers short answers."})
    monkeypatch.setattr(ai_calls, "get_atagia_bridge", lambda: bridge)

    prompt = await ai_calls._augment_prompt_with_atagia_context(
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
        }
    ]
    assert "Base system prompt" in prompt
    assert "[ATAGIA MEMORY CONTEXT - INTERNAL]" in prompt
    assert "Memory says: prefers short answers." in prompt


@pytest.mark.asyncio
async def test_record_atagia_assistant_response_flattens_multi_ai_payload(monkeypatch):
    import ai_calls

    bridge = FakeBridge()
    monkeypatch.setattr(ai_calls, "get_atagia_bridge", lambda: bridge)
    combined_response = orjson.dumps(
        {
            "multi_ai": True,
            "responses": [
                {"model": "gpt-5", "content": "First answer"},
                {"model": "claude", "content": "Second answer"},
            ],
        }
    ).decode()

    recorded = await ai_calls._record_atagia_assistant_response(
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
        }
    ]


@pytest.mark.asyncio
async def test_warmup_snapshot_primes_atagia_conversation_without_draft_text(monkeypatch):
    import ai_calls
    from chat_warmup import WarmupCacheKey

    warmup_calls = []

    async def fake_context_messages(conversation_id, start_date):
        return [{"message": "previous", "type": "user"}]

    async def fake_prompt_runtime(conversation_id, current_user, effective_prompt_id):
        return {"full_prompt": "runtime prompt"}

    async def fake_warmup_atagia(user_id, conversation_id):
        warmup_calls.append((user_id, conversation_id))
        return True

    monkeypatch.setattr(ai_calls, "_load_warmup_context_messages", fake_context_messages)
    monkeypatch.setattr(ai_calls, "_load_warmup_prompt_runtime_snapshot", fake_prompt_runtime)
    monkeypatch.setattr(ai_calls, "_warmup_atagia_sidecar", fake_warmup_atagia)

    key = WarmupCacheKey(
        user_id=7,
        conversation_id=99,
        llm_id=1,
        effective_prompt_id=2,
        active_extension_id=0,
        last_message_id=10,
        mode="single",
    )
    snapshot = await ai_calls._build_chat_warmup_snapshot(
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

    assert warmup_calls == [(7, 99)]
    assert snapshot["context_messages"] == [{"message": "previous", "type": "user"}]
    assert snapshot["sidecars"] == {"atagia_ready": True}
