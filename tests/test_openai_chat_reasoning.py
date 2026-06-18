import orjson
import pytest

from ai_runtime.providers import openai_chat


class _User:
    id = 1


class _FakeContent:
    def __init__(self, payload: str):
        self._payload = payload.encode("utf-8")

    async def iter_chunked(self, _size):
        yield self._payload


class _FakeResponse:
    status = 200

    def __init__(self, payload: str):
        self.content = _FakeContent(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""

    async def json(self):
        return {}


class _FakeSession:
    def __init__(self, payload: str, captured: dict):
        self._payload = payload
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, _url, headers=None, json=None):
        self._captured["headers"] = headers or {}
        self._captured["json"] = json or {}
        return _FakeResponse(self._payload)


def _sse_payload(event: str) -> dict:
    assert event.startswith("data: ")
    return orjson.loads(event[6:].strip())


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_streams_as_thinking(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"more"}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="Kimi",
            save_to_db=False,
            omit_temperature=True,
        )
    ]

    parsed = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert parsed[:4] == [
        {"type": "thinking_start"},
        {"thinking": "think ", "type": "thinking"},
        {"thinking": "more", "type": "thinking"},
        {"type": "thinking_end"},
    ]
    assert parsed[4] == {"content": "answer"}
    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_openai_compatible_tool_call_preserves_reasoning_content(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_content":"need a search"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","function":{"name":"query_perplexity","arguments":"{\\"query\\":\\"docs\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="Kimi",
            user_message="hi",
            save_to_db=True,
            omit_temperature=True,
        )
    ]

    tool_events = [
        _sse_payload(chunk)["tool_call"]
        for chunk in chunks
        if chunk.startswith("data: {") and "tool_call" in chunk and "tool_call_pending" not in chunk
    ]
    assert tool_events == [{
        "name": "query_perplexity",
        "arguments": {"query": "docs"},
        "id": "call_1",
        "reasoning_content": "need a search",
    }]


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_details_use_cumulative_delta(monkeypatch):
    payload = "\n\n".join([
        'data: {"choices":[{"delta":{"reasoning_details":[{"text":"think"}]}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"text":"thinking"}]}}]}',
        'data: {"choices":[{"delta":{"content":"done"}}]}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        openai_chat.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in openai_chat.call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="MiniMax-M3",
            temperature=1.0,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            api_url="https://example.invalid/v1/chat/completions",
            api_key="test",
            provider_label="MiniMax",
            save_to_db=False,
        )
    ]

    parsed = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert parsed[:4] == [
        {"type": "thinking_start"},
        {"thinking": "think", "type": "thinking"},
        {"thinking": "ing", "type": "thinking"},
        {"type": "thinking_end"},
    ]
