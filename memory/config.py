from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
import time
from typing import Any, Literal
from urllib.parse import urlparse

import orjson

import database
from log_config import logger


MemoryProviderName = Literal["none", "atagia", "mem0"]
MemoryScope = Literal["global", "prompt"]

VALID_MEMORY_PROVIDERS: set[str] = {"none", "atagia", "mem0"}
VALID_MEMORY_SCOPES: set[str] = {"global", "prompt"}
DEFAULT_MEMORY_SCOPE: MemoryScope = "prompt"
DEFAULT_MEM0_BASE_URL = "http://127.0.0.1:8888"
DEFAULT_MEM0_PLATFORM_ID = "aurvek-local"
DEFAULT_MEM0_TIMEOUT_SECONDS = 30.0
DEFAULT_MEM0_TOP_K = 8
DEFAULT_NONE_CONTEXT_MAX_TOKENS = 128000
MAX_NONE_CONTEXT_MAX_TOKENS = 2_000_000
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_CONFIG_CACHE_TTL_SECONDS = 300
_config_cache: dict[str, str] | None = None
_config_cache_time = 0.0


@dataclass(frozen=True, slots=True)
class MemoryRuntimeConfig:
    active_provider: MemoryProviderName
    default_scope: MemoryScope = DEFAULT_MEMORY_SCOPE


@dataclass(frozen=True, slots=True)
class Mem0Config:
    base_url: str = DEFAULT_MEM0_BASE_URL
    api_key: str | None = None
    platform_id: str = DEFAULT_MEM0_PLATFORM_ID
    timeout_seconds: float = DEFAULT_MEM0_TIMEOUT_SECONDS
    top_k: int = DEFAULT_MEM0_TOP_K


def _env_defaults() -> dict[str, str]:
    return {
        "memory_active_provider": _clean(os.getenv("MEMORY_ACTIVE_PROVIDER")) or "",
        "memory_default_scope": _clean(os.getenv("MEMORY_DEFAULT_SCOPE")) or DEFAULT_MEMORY_SCOPE,
        "mem0_base_url": _clean(os.getenv("MEM0_BASE_URL")) or DEFAULT_MEM0_BASE_URL,
        "mem0_api_key": _clean(os.getenv("MEM0_API_KEY")) or "",
        "mem0_platform_id": (
            _clean(os.getenv("MEM0_PLATFORM_ID"))
            or _clean(os.getenv("AURVEK_INSTANCE_ID"))
            or DEFAULT_MEM0_PLATFORM_ID
        ),
        "mem0_timeout_seconds": _clean(os.getenv("MEM0_TIMEOUT_SECONDS")) or str(DEFAULT_MEM0_TIMEOUT_SECONDS),
        "mem0_top_k": _clean(os.getenv("MEM0_TOP_K")) or str(DEFAULT_MEM0_TOP_K),
        "memory_none_context_max_tokens": (
            _clean(os.getenv("MEMORY_NONE_CONTEXT_MAX_TOKENS"))
            or str(DEFAULT_NONE_CONTEXT_MAX_TOKENS)
        ),
        "memory_none_context_exceptions": (
            _clean(os.getenv("MEMORY_NONE_CONTEXT_EXCEPTIONS")) or "[]"
        ),
    }


async def get_memory_config() -> dict[str, str]:
    """Load generic memory config from SYSTEM_CONFIG, env, and safe defaults."""
    global _config_cache, _config_cache_time

    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_CACHE_TTL_SECONDS:
        return dict(_config_cache)

    config = _env_defaults()
    try:
        async with database.get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT key, value FROM SYSTEM_CONFIG
                WHERE key LIKE 'memory\\_%' ESCAPE '\\'
                   OR key LIKE 'mem0\\_%' ESCAPE '\\'
                """
            )
            rows = await cursor.fetchall()
            for row in rows:
                config[str(row[0])] = "" if row[1] is None else str(row[1])
    except Exception as exc:
        logger.error("Failed to load memory config from DB: %s", exc)

    normalized = await _normalize_config(config)
    _config_cache = normalized
    _config_cache_time = now
    return dict(normalized)


async def get_memory_runtime_config() -> MemoryRuntimeConfig:
    config = await get_memory_config()
    return MemoryRuntimeConfig(
        active_provider=_parse_provider(config.get("memory_active_provider")),
        default_scope=_parse_scope(config.get("memory_default_scope")),
    )


async def get_active_memory_provider() -> MemoryProviderName:
    return (await get_memory_runtime_config()).active_provider


async def get_mem0_config() -> Mem0Config:
    config = await get_memory_config()
    return mem0_config_from_mapping(config)


def mem0_config_from_mapping(config: dict[str, Any]) -> Mem0Config:
    return Mem0Config(
        base_url=_clean(config.get("mem0_base_url")) or DEFAULT_MEM0_BASE_URL,
        api_key=_clean(config.get("mem0_api_key")),
        platform_id=_parse_platform_id(config.get("mem0_platform_id")),
        timeout_seconds=_parse_timeout(config.get("mem0_timeout_seconds")),
        top_k=_parse_top_k(config.get("mem0_top_k")),
    )


async def save_memory_admin_config(payload: dict[str, Any]) -> dict[str, str]:
    """Persist generic active-provider and default-scope settings."""
    current = await get_memory_config()
    provider = _parse_provider(payload.get("active_provider", current.get("memory_active_provider")))
    scope = _parse_scope(payload.get("default_scope", current.get("memory_default_scope")))
    updates = {
        "memory_active_provider": provider,
        "memory_default_scope": scope,
    }

    async with database.get_db_connection() as conn:
        columns = await _system_config_columns(conn)
        for key, value in updates.items():
            await _upsert_system_config(conn, columns, key, value)

        if provider == "atagia":
            await _upsert_system_config(conn, columns, "atagia_enabled", "true")
        await conn.commit()

    invalidate_memory_config_cache()
    try:
        from atagia_config import invalidate_atagia_config_cache

        invalidate_atagia_config_cache()
    except Exception:
        pass
    return {key: (await get_memory_config()).get(key, "") for key in updates}


async def save_mem0_admin_config(payload: dict[str, Any]) -> dict[str, str]:
    """Persist Mem0 self-hosted REST settings. Blank API key preserves saved secret."""
    current = await get_memory_config()
    base_url = _clean(payload.get("base_url", current.get("mem0_base_url"))) or DEFAULT_MEM0_BASE_URL
    ok, message = validate_mem0_base_url(base_url)
    if not ok:
        raise ValueError(message)

    timeout = str(_parse_timeout(payload.get("timeout_seconds", current.get("mem0_timeout_seconds"))))
    top_k = str(_parse_top_k(payload.get("top_k", current.get("mem0_top_k"))))
    updates = {
        "mem0_base_url": base_url.rstrip("/"),
        "mem0_platform_id": _parse_platform_id(payload.get("platform_id", current.get("mem0_platform_id"))),
        "mem0_timeout_seconds": timeout,
        "mem0_top_k": top_k,
    }
    api_key = _clean(payload.get("api_key"))
    clear_api_key = _parse_bool(payload.get("clear_api_key"))
    if api_key or clear_api_key:
        updates["mem0_api_key"] = api_key or ""

    async with database.get_db_connection() as conn:
        columns = await _system_config_columns(conn)
        for key, value in updates.items():
            await _upsert_system_config(conn, columns, key, value)
        await conn.commit()

    invalidate_memory_config_cache()
    fresh = await get_memory_config()
    return {key: fresh.get(key, "") for key in updates}


async def save_no_memory_context_config(payload: dict[str, Any]) -> dict[str, str]:
    """Persist local recent-history context limits used when provider is none."""
    current = await get_memory_config()
    if "max_tokens" in payload:
        max_tokens = _parse_context_max_tokens(payload.get("max_tokens"), default=None)
    else:
        max_tokens = _parse_context_max_tokens(
            current.get("memory_none_context_max_tokens"),
            default=DEFAULT_NONE_CONTEXT_MAX_TOKENS,
        )
    exceptions = _parse_context_exceptions(
        payload.get("exceptions", current.get("memory_none_context_exceptions", "[]"))
    )
    updates = {
        "memory_none_context_max_tokens": str(max_tokens),
        "memory_none_context_exceptions": orjson.dumps(exceptions).decode("utf-8"),
    }

    async with database.get_db_connection() as conn:
        columns = await _system_config_columns(conn)
        for key, value in updates.items():
            await _upsert_system_config(conn, columns, key, value)
        await conn.commit()

    invalidate_memory_config_cache()
    fresh = await get_memory_config()
    return {key: fresh.get(key, "") for key in updates}


async def reset_mem0_admin_config() -> dict[str, str]:
    async with database.get_db_connection() as conn:
        await conn.execute(
            """
            DELETE FROM SYSTEM_CONFIG
            WHERE key LIKE 'mem0\\_%' ESCAPE '\\'
              AND key <> 'mem0_platform_id'
            """
        )
        await conn.commit()
    invalidate_memory_config_cache()
    return await get_memory_config()


def template_memory_config(config: dict[str, str]) -> dict[str, Any]:
    api_key = config.get("mem0_api_key", "")
    return {
        "active_provider": _parse_provider(config.get("memory_active_provider")),
        "default_scope": _parse_scope(config.get("memory_default_scope")),
        "none_context": {
            "max_tokens": _parse_context_max_tokens(
                config.get("memory_none_context_max_tokens"),
                default=DEFAULT_NONE_CONTEXT_MAX_TOKENS,
            ),
            "exceptions": _parse_context_exceptions(
                config.get("memory_none_context_exceptions")
            ),
        },
        "mem0": {
            "base_url": config.get("mem0_base_url", DEFAULT_MEM0_BASE_URL),
            "platform_id": _parse_platform_id(config.get("mem0_platform_id")),
            "timeout_seconds": config.get("mem0_timeout_seconds", str(DEFAULT_MEM0_TIMEOUT_SECONDS)),
            "top_k": config.get("mem0_top_k", str(DEFAULT_MEM0_TOP_K)),
            "has_api_key": bool(api_key),
            "api_key_masked": mask_secret(api_key),
        },
    }


async def get_user_memory_preferences(
    user_id: int | str,
    provider: str | None = None,
) -> dict[str, Any]:
    runtime = await get_memory_runtime_config()
    provider_name = _parse_provider(provider or runtime.active_provider)
    if provider_name == "none":
        return {
            "provider": "none",
            "available": False,
            "remember_across_chats": False,
            "memory_scope": runtime.default_scope,
            "message": "Memory is disabled by the administrator.",
        }

    await ensure_memory_preference_schema()
    async with database.get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT remember_across_chats, memory_scope, settings_json
            FROM MEMORY_USER_PREFERENCES
            WHERE user_id = ? AND provider = ?
            """,
            (int(user_id), provider_name),
        )
        row = await cursor.fetchone()

    if not row:
        return {
            "provider": provider_name,
            "available": True,
            "remember_across_chats": True,
            "memory_scope": runtime.default_scope,
            "settings": {},
        }

    settings = {}
    try:
        settings = orjson.loads(row[2] or "{}")
        if not isinstance(settings, dict):
            settings = {}
    except Exception:
        settings = {}
    return {
        "provider": provider_name,
        "available": True,
        "remember_across_chats": bool(row[0]),
        "memory_scope": _parse_scope(row[1]),
        "settings": settings,
    }


async def save_user_memory_preferences(
    user_id: int | str,
    provider: str,
    *,
    remember_across_chats: bool | None = None,
    memory_scope: str | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_name = _parse_provider(provider)
    if provider_name == "none":
        raise ValueError("Cannot save preferences for disabled memory provider.")

    current = await get_user_memory_preferences(user_id, provider_name)
    resolved_remember = (
        bool(remember_across_chats)
        if remember_across_chats is not None
        else bool(current.get("remember_across_chats", True))
    )
    resolved_scope = (
        _parse_scope(memory_scope)
        if memory_scope is not None
        else _parse_scope(current.get("memory_scope"))
    )
    resolved_settings = dict(current.get("settings") or {})
    if settings:
        resolved_settings.update(settings)

    await ensure_memory_preference_schema()
    async with database.get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO MEMORY_USER_PREFERENCES
                (user_id, provider, remember_across_chats, memory_scope, settings_json, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                remember_across_chats = excluded.remember_across_chats,
                memory_scope = excluded.memory_scope,
                settings_json = excluded.settings_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(user_id),
                provider_name,
                1 if resolved_remember else 0,
                resolved_scope,
                orjson.dumps(resolved_settings).decode("utf-8"),
            ),
        )
        await conn.commit()

    return await get_user_memory_preferences(user_id, provider_name)


async def get_user_memory_scope(user_id: int | str, provider: str) -> MemoryScope:
    preferences = await get_user_memory_preferences(user_id, provider)
    return _parse_scope(preferences.get("memory_scope"))


async def resolve_no_memory_context_max_tokens(
    *,
    llm_id: int | str | None = None,
    prompt_id: int | str | None = None,
) -> tuple[int, str]:
    """Return local history budget for provider=none and its source label."""
    config = await get_memory_config()
    exceptions = _parse_context_exceptions(config.get("memory_none_context_exceptions"))
    normalized_prompt_id = _parse_positive_int(prompt_id)
    normalized_llm_id = _parse_positive_int(llm_id)

    # Prompt exceptions win over model exceptions when both conditions match.
    if normalized_prompt_id is not None:
        for exception in exceptions:
            if exception["type"] == "prompt" and exception["id"] == normalized_prompt_id:
                return int(exception["max_tokens"]), "prompt"

    if normalized_llm_id is not None:
        for exception in exceptions:
            if exception["type"] == "llm" and exception["id"] == normalized_llm_id:
                return int(exception["max_tokens"]), "llm"

    return (
        _parse_context_max_tokens(
            config.get("memory_none_context_max_tokens"),
            default=DEFAULT_NONE_CONTEXT_MAX_TOKENS,
        ),
        "global",
    )


async def ensure_memory_preference_schema() -> None:
    async with database.get_db_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS MEMORY_USER_PREFERENCES (
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                remember_across_chats INTEGER NOT NULL DEFAULT 1,
                memory_scope TEXT NOT NULL DEFAULT 'prompt',
                settings_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, provider)
            )
            """
        )
        await conn.commit()


def invalidate_memory_config_cache() -> None:
    global _config_cache, _config_cache_time
    _config_cache = None
    _config_cache_time = 0.0


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) < 16:
        return "****"
    return f"{value[:8]}...{value[-4:]}"


def validate_mem0_base_url(url: str) -> tuple[bool, str]:
    if not url:
        return False, "Mem0 base URL is required."
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Mem0 base URL must use http:// or https://."
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "Mem0 base URL must include a host."
    if host == "api.mem0.ai" or host.endswith(".mem0.ai"):
        return False, "Hosted Mem0 Platform URLs are not allowed. Use a self-hosted OSS server."

    if host in {"localhost", "host.docker.internal"}:
        return True, ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, "Use localhost, host.docker.internal, or a private IP for the Mem0 OSS server."
    if ip.is_loopback or ip.is_private:
        return True, ""
    return False, "Mem0 OSS server must be local or on a private network."


async def _normalize_config(config: dict[str, str]) -> dict[str, str]:
    normalized = dict(_env_defaults())
    normalized.update(config)

    active_provider = _clean(normalized.get("memory_active_provider"))
    if not active_provider:
        active_provider = "atagia" if await _legacy_atagia_enabled() else "none"
    normalized["memory_active_provider"] = _parse_provider(active_provider)
    normalized["memory_default_scope"] = _parse_scope(normalized.get("memory_default_scope"))
    mem0_base_url = (_clean(normalized.get("mem0_base_url")) or DEFAULT_MEM0_BASE_URL).rstrip("/")
    mem0_url_ok, mem0_url_error = validate_mem0_base_url(mem0_base_url)
    if not mem0_url_ok:
        logger.warning(
            "Ignoring invalid Mem0 base URL from configuration: %s",
            mem0_url_error,
        )
        mem0_base_url = DEFAULT_MEM0_BASE_URL
    normalized["mem0_base_url"] = mem0_base_url
    normalized["mem0_api_key"] = _clean(normalized.get("mem0_api_key")) or ""
    normalized["mem0_platform_id"] = _parse_platform_id(normalized.get("mem0_platform_id"))
    normalized["mem0_timeout_seconds"] = str(_parse_timeout(normalized.get("mem0_timeout_seconds")))
    normalized["mem0_top_k"] = str(_parse_top_k(normalized.get("mem0_top_k")))
    try:
        none_context_max_tokens = _parse_context_max_tokens(
            normalized.get("memory_none_context_max_tokens"),
            default=DEFAULT_NONE_CONTEXT_MAX_TOKENS,
        )
    except ValueError as exc:
        logger.warning("Ignoring invalid no-memory context token limit: %s", exc)
        none_context_max_tokens = DEFAULT_NONE_CONTEXT_MAX_TOKENS
    normalized["memory_none_context_max_tokens"] = str(none_context_max_tokens)
    try:
        context_exceptions = _parse_context_exceptions(
            normalized.get("memory_none_context_exceptions")
        )
    except ValueError as exc:
        logger.warning("Ignoring invalid no-memory context exceptions: %s", exc)
        context_exceptions = []
    normalized["memory_none_context_exceptions"] = orjson.dumps(context_exceptions).decode("utf-8")
    return normalized


async def _legacy_atagia_enabled() -> bool:
    value = os.getenv("ATAGIA_ENABLED")
    try:
        async with database.get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT value FROM SYSTEM_CONFIG WHERE key = ?",
                ("atagia_enabled",),
            )
            row = await cursor.fetchone()
            if row:
                value = row[0]
    except Exception:
        pass
    return _parse_bool(value)


async def _system_config_columns(conn: Any) -> set[str]:
    cursor = await conn.execute("PRAGMA table_info(SYSTEM_CONFIG)")
    rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


async def _upsert_system_config(conn: Any, columns: set[str], key: str, value: str) -> None:
    if "updated_at" in columns:
        await conn.execute(
            "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
            (value, key),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
    else:
        await conn.execute("UPDATE SYSTEM_CONFIG SET value = ? WHERE key = ?", (value, key))
        await conn.execute("INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)", (key, value))


def _parse_provider(value: Any) -> MemoryProviderName:
    provider = str(value or "none").strip().lower()
    return provider if provider in VALID_MEMORY_PROVIDERS else "none"  # type: ignore[return-value]


def _parse_scope(value: Any) -> MemoryScope:
    scope = str(value or DEFAULT_MEMORY_SCOPE).strip().lower()
    return scope if scope in VALID_MEMORY_SCOPES else DEFAULT_MEMORY_SCOPE  # type: ignore[return-value]


def _parse_platform_id(value: Any) -> str:
    text = _clean(value) or DEFAULT_MEM0_PLATFORM_ID
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("._-")
    return (safe or DEFAULT_MEM0_PLATFORM_ID)[:64]


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def _parse_timeout(value: Any) -> float:
    try:
        timeout = float(str(value or "").strip())
    except ValueError:
        return DEFAULT_MEM0_TIMEOUT_SECONDS
    if timeout <= 0:
        return DEFAULT_MEM0_TIMEOUT_SECONDS
    return timeout


def _parse_top_k(value: Any) -> int:
    try:
        top_k = int(str(value or "").strip())
    except ValueError:
        return DEFAULT_MEM0_TOP_K
    return min(max(top_k, 1), 50)


def _parse_positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_context_max_tokens(
    value: Any,
    *,
    default: int | None = None,
) -> int:
    if value is None or str(value).strip() == "":
        if default is None:
            raise ValueError("Context max tokens are required.")
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        if default is None:
            raise ValueError("Context max tokens must be a whole number.") from exc
        return default
    if parsed < 0:
        raise ValueError("Context max tokens cannot be negative.")
    if parsed > MAX_NONE_CONTEXT_MAX_TOKENS:
        raise ValueError(
            f"Context max tokens cannot exceed {MAX_NONE_CONTEXT_MAX_TOKENS}."
        )
    return parsed


def _parse_context_exceptions(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            parsed: Any = []
        else:
            try:
                parsed = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                raise ValueError("Context exceptions must be valid JSON.") from exc
    else:
        parsed = value or []

    if not isinstance(parsed, list):
        raise ValueError("Context exceptions must be a list.")

    normalized: dict[tuple[str, int], dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Each context exception must be an object.")
        exception_type = str(item.get("type") or item.get("kind") or "").strip().lower()
        if exception_type not in {"llm", "prompt"}:
            raise ValueError("Context exception type must be llm or prompt.")
        target_id = _parse_positive_int(item.get("id") or item.get("target_id"))
        if target_id is None:
            raise ValueError("Context exception target id must be a positive integer.")
        max_tokens = _parse_context_max_tokens(item.get("max_tokens"), default=None)
        key = (exception_type, target_id)
        if key in normalized:
            del normalized[key]
        entry = {
            "type": exception_type,
            "id": target_id,
            "max_tokens": max_tokens,
        }
        label = _clean(item.get("label"))
        if label:
            entry["label"] = label
        normalized[key] = entry
    return list(normalized.values())


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
