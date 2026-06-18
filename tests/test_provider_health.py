import asyncio

import aiohttp
import pytest

from ai_runtime import provider_health
from ai_runtime.errors import _provider_error_payload


def test_provider_error_classification_separates_normal_and_suspicious_errors():
    assert provider_health.classify_provider_error("quota exceeded", status_code=429).suspicious is False
    assert provider_health.classify_provider_error("PDF too large", status_code=413).suspicious is False
    assert provider_health.classify_provider_error("upstream unavailable", status_code=503).suspicious is True
    assert provider_health.classify_provider_error(
        "connection failed",
        exception=aiohttp.ClientConnectionError("connection failed"),
    ).suspicious is True


@pytest.mark.asyncio
async def test_provider_health_transitions_from_suspicious_errors_to_recovery(monkeypatch):
    provider_health.reset_provider_health_state()
    monkeypatch.setattr(provider_health, "maybe_schedule_provider_check", lambda *args, **kwargs: None)

    now = [1000.0]
    monkeypatch.setattr(provider_health.time, "time", lambda: now[0])

    for offset in (0, 10, 20):
        now[0] = 1000.0 + offset
        health = await provider_health.record_provider_error(
            "openai",
            message="connection error",
            exception=aiohttp.ClientConnectionError("connection error"),
        )

    assert health["status"] == provider_health.PROVIDER_SUSPECTED
    assert health["surface"] is True

    for offset in (30, 40):
        now[0] = 1000.0 + offset
        health = await provider_health.record_provider_error(
            "openai",
            message="service unavailable",
            status_code=503,
        )

    assert health["status"] == provider_health.PROVIDER_DEGRADED

    for offset in (400, 410, 420):
        now[0] = 1000.0 + offset
        health = await provider_health.record_provider_success("openai")

    assert health["status"] == provider_health.PROVIDER_OPERATIONAL
    assert health["surface"] is False


@pytest.mark.asyncio
async def test_provider_error_payload_includes_health_metadata_when_degraded(monkeypatch):
    provider_health.reset_provider_health_state()
    monkeypatch.setattr(provider_health, "maybe_schedule_provider_check", lambda *args, **kwargs: None)

    for _ in range(3):
        await provider_health.record_provider_error(
            "openai",
            message="connection error",
            exception=aiohttp.ClientConnectionError("connection error"),
        )

    payload = _provider_error_payload("OpenAI (GPT)", "OpenAI connection error.")

    assert payload["error"] == "OpenAI connection error."
    assert payload["provider"] == "openai"
    assert payload["provider_status"] == provider_health.PROVIDER_SUSPECTED
    assert payload["provider_health"]["provider_name"] == "OpenAI"


@pytest.mark.asyncio
async def test_provider_health_clears_local_status_after_error_window(monkeypatch):
    provider_health.reset_provider_health_state()
    monkeypatch.setattr(provider_health, "maybe_schedule_provider_check", lambda *args, **kwargs: None)

    now = [1000.0]
    monkeypatch.setattr(provider_health.time, "time", lambda: now[0])

    for offset in (0, 10, 20):
        now[0] = 1000.0 + offset
        health = await provider_health.record_provider_error(
            "openai",
            message="connection error",
            exception=aiohttp.ClientConnectionError("connection error"),
        )

    assert health["status"] == provider_health.PROVIDER_SUSPECTED

    now[0] = 1321.0
    health = provider_health.get_provider_health("openai")

    assert health["status"] == provider_health.PROVIDER_OPERATIONAL
    assert health["surface"] is False


@pytest.mark.asyncio
async def test_external_error_note_only_appends_when_provider_health_surfaces(monkeypatch):
    provider_health.reset_provider_health_state()
    monkeypatch.setattr(provider_health, "maybe_schedule_provider_check", lambda *args, **kwargs: None)

    original = "Could not generate a response."
    assert provider_health.append_external_error_note(original, "openai") == original

    for _ in range(3):
        await provider_health.record_provider_error(
            "openai",
            message="timeout",
            exception=asyncio.TimeoutError(),
        )

    noted = provider_health.append_external_error_note(original, "openai")

    assert original in noted
    assert "selected AI provider (OpenAI)" in noted
