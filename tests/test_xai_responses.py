import orjson
import pytest

from ai_runtime.providers import xai


class _User:
    id = 1


class _FakeContent:
    def __init__(self, payload: str):
        self._payload = payload.encode("utf-8")

    async def iter_any(self):
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
async def test_xai_responses_converts_messages_and_streams_text_delta(monkeypatch):
    payload = "\n\n".join([
        'event: response.text.delta\ndata: {"delta":"hello"}',
        'event: response.completed\ndata: {"response":{"usage":{"input_tokens":2,"output_tokens":3}}}',
        "data: [DONE]",
        "",
    ])
    captured = {}
    monkeypatch.setattr(
        xai.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(payload, captured),
    )

    chunks = [
        chunk async for chunk in xai.call_xai_responses_api(
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            model="grok-4.3",
            temperature=0.7,
            max_tokens=100,
            prompt="system",
            conversation_id=123,
            current_user=_User(),
            request=None,
            user_api_key="test",
            save_to_db=False,
        )
    ]

    parsed = [_sse_payload(chunk) for chunk in chunks if chunk.startswith("data: {")]
    assert parsed[0] == {"content": "hello"}
    assert captured["json"]["input"][0] == {"role": "system", "content": "system"}
    assert captured["json"]["input"][1]["content"] == [{"type": "input_text", "text": "hi"}]
