import orjson
import pytest


class DummyRequest:
    cookies = {"session": "valid-token"}


class DummyUser:
    id = 7


def response_json(response):
    return orjson.loads(response.body)


@pytest.fixture
def warmup_endpoint_mocks(monkeypatch):
    import ai_calls

    monkeypatch.setattr(ai_calls, "decode_jwt_cached", lambda token, secret: {"sub": "7"})
    monkeypatch.setattr(ai_calls, "verify_token_expiration", lambda payload: True)

    async def allow_rate_limit(*args, **kwargs):
        return True

    async def fake_state(*args, **kwargs):
        return {
            "locked": 0,
            "llm_id": 1,
            "effective_prompt_id": 2,
            "active_extension_id": 0,
            "last_message_id": 42,
        }

    async def fake_get_or_prepare(cache_key, prepare_snapshot):
        return {"context_count": 3}, "hit"

    monkeypatch.setattr(ai_calls, "check_rate_limit", allow_rate_limit)
    monkeypatch.setattr(ai_calls, "_load_warmup_conversation_state", fake_state)
    monkeypatch.setattr(ai_calls, "warmup_get_or_prepare", fake_get_or_prepare)
    return ai_calls


@pytest.mark.asyncio
async def test_warmup_endpoint_requires_auth(warmup_endpoint_mocks):
    response = await warmup_endpoint_mocks.warmup_conversation_context(
        DummyRequest(),
        conversation_id=123,
        current_user=None,
        payload={"activity": "typing"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_warmup_endpoint_rejects_invalid_activity(warmup_endpoint_mocks):
    response = await warmup_endpoint_mocks.warmup_conversation_context(
        DummyRequest(),
        conversation_id=123,
        current_user=DummyUser(),
        payload={"activity": "reading"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_warmup_endpoint_rate_limit(monkeypatch, warmup_endpoint_mocks):
    async def deny_rate_limit(*args, **kwargs):
        return False

    async def fake_rate_status(*args, **kwargs):
        return {"limit": 30, "current": 31}

    monkeypatch.setattr(warmup_endpoint_mocks, "check_rate_limit", deny_rate_limit)
    monkeypatch.setattr(warmup_endpoint_mocks, "get_rate_limit_status", fake_rate_status)

    response = await warmup_endpoint_mocks.warmup_conversation_context(
        DummyRequest(),
        conversation_id=123,
        current_user=DummyUser(),
        payload={"activity": "typing"},
    )

    assert response.status_code == 429
    assert response_json(response)["reason"] == "rate_limited"


@pytest.mark.asyncio
async def test_warmup_endpoint_blocks_locked_conversation(monkeypatch, warmup_endpoint_mocks):
    async def locked_state(*args, **kwargs):
        return {"locked": 1}

    monkeypatch.setattr(warmup_endpoint_mocks, "_load_warmup_conversation_state", locked_state)

    response = await warmup_endpoint_mocks.warmup_conversation_context(
        DummyRequest(),
        conversation_id=123,
        current_user=DummyUser(),
        payload={"activity": "typing"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_warmup_endpoint_returns_hit_without_draft_text(warmup_endpoint_mocks):
    response = await warmup_endpoint_mocks.warmup_conversation_context(
        DummyRequest(),
        conversation_id=123,
        current_user=DummyUser(),
        payload={
            "activity": "typing",
            "draft_length": 25,
            "multi_ai_model_ids": [2, 3],
            "last_known_message_id": 41,
        },
    )

    payload = response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "hit"
    assert payload["mode"] == "multi"
    assert payload["last_message_id"] == 42
    assert "draft" not in orjson.dumps(payload).decode()
