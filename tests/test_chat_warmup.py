import asyncio

import pytest
from cachetools import TTLCache

import chat.services.warmup as chat_warmup
from chat.services.warmup import (
    WarmupCacheKey,
    clear_warmup_cache,
    get_or_prepare,
    get_snapshot,
    normalize_model_ids,
    put_snapshot,
)


def make_key(
    *,
    last_message_id: int = 10,
    llm_id: int = 1,
    prompt_id: int = 2,
    extension_id: int = 0,
    mode: str = "single",
    multi_ids: tuple[int, ...] = (),
) -> WarmupCacheKey:
    return WarmupCacheKey(
        user_id=7,
        conversation_id=99,
        llm_id=llm_id,
        effective_prompt_id=prompt_id,
        active_extension_id=extension_id,
        last_message_id=last_message_id,
        mode=mode,
        multi_ai_model_ids=multi_ids,
    )


@pytest.fixture(autouse=True)
def reset_warmup_cache():
    clear_warmup_cache()
    yield
    clear_warmup_cache()


def test_warmup_cache_hit_miss_and_key_invalidation():
    key = make_key()
    snapshot = {"context_messages": [{"type": "user", "message": "hola"}]}

    assert get_snapshot(key) is None

    put_snapshot(key, snapshot)
    assert get_snapshot(key) == snapshot

    assert get_snapshot(make_key(last_message_id=11)) is None
    assert get_snapshot(make_key(llm_id=3)) is None
    assert get_snapshot(make_key(prompt_id=4)) is None
    assert get_snapshot(make_key(extension_id=5)) is None
    assert get_snapshot(make_key(mode="multi", multi_ids=(1, 2))) is None


@pytest.mark.asyncio
async def test_warmup_cache_ttl_expiry(monkeypatch):
    monkeypatch.setattr(chat_warmup, "_warmup_cache", TTLCache(maxsize=4, ttl=0.01))
    clear_warmup_cache()

    key = make_key()
    put_snapshot(key, {"context_messages": []})
    assert get_snapshot(key) is not None

    await asyncio.sleep(0.02)
    assert get_snapshot(key) is None


@pytest.mark.asyncio
async def test_warmup_singleflight_prepares_once():
    key = make_key()
    calls = 0

    async def prepare():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"context_messages": [], "context_count": 0}

    results = await asyncio.gather(*[get_or_prepare(key, prepare) for _ in range(5)])

    assert calls == 1
    statuses = [status for _, status in results]
    assert statuses.count("prepared") == 1
    assert statuses.count("hit") == 4
    assert all(snapshot is not None for snapshot, _ in results)


def test_normalize_model_ids_preserves_order_and_deduplicates():
    assert normalize_model_ids([3, "2", 3, 0, "bad", 5]) == (3, 2, 5)
    assert normalize_model_ids("1,2,3") == ()
    assert normalize_model_ids(None) == ()
