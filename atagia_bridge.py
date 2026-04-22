"""Thin Aurvek adapter for Atagia sidecar memory integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import os
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

TransportName = Literal["auto", "local", "http"]
DEFAULT_ASSISTANT_MODE = "personal_assistant"
DEFAULT_TIMEOUT_SECONDS = 30.0
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_VALID_TRANSPORTS: set[str] = {"auto", "local", "http"}


class AtagiaClientProtocol(Protocol):
    """Subset of the generic Atagia client used by Aurvek."""

    async def create_user(self, user_id: str) -> None:
        """Create the user if needed."""

    async def create_conversation(
        self,
        user_id: str,
        conversation_id: str | None,
        workspace_id: str | None = None,
        assistant_mode_id: str | None = None,
    ) -> str:
        """Create or reuse an Atagia conversation."""

    async def get_context(
        self,
        user_id: str,
        conversation_id: str,
        message: str,
        mode: str | None = None,
        workspace_id: str | None = None,
        occurred_at: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Return memory context for a host-managed LLM call."""

    async def add_response(
        self,
        user_id: str,
        conversation_id: str,
        text: str,
        occurred_at: str | None = None,
    ) -> None:
        """Persist a host-generated assistant response."""

    async def close(self) -> None:
        """Close transport resources."""


ClientFactory = Callable[..., Awaitable[AtagiaClientProtocol]]


@dataclass(frozen=True, slots=True)
class AtagiaBridgeConfig:
    """Environment-backed Atagia bridge settings."""

    enabled: bool = False
    transport: TransportName = "auto"
    db_path: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    assistant_mode: str = DEFAULT_ASSISTANT_MODE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AtagiaBridgeConfig":
        env = environ or os.environ
        transport = _parse_transport(env.get("ATAGIA_TRANSPORT", "auto"))
        return cls(
            enabled=_parse_bool(env.get("ATAGIA_ENABLED")),
            transport=transport,
            db_path=_clean_optional(env.get("ATAGIA_DB_PATH")),
            base_url=_clean_optional(env.get("ATAGIA_BASE_URL")),
            api_key=_clean_optional(env.get("ATAGIA_SERVICE_API_KEY")),
            assistant_mode=(
                _clean_optional(env.get("ATAGIA_ASSISTANT_MODE"))
                or DEFAULT_ASSISTANT_MODE
            ),
            timeout_seconds=_parse_timeout(env.get("ATAGIA_TIMEOUT_SECONDS")),
        )


class AtagiaBridge:
    """Aurvek-specific Atagia adapter with fail-open behavior."""

    def __init__(
        self,
        config: AtagiaBridgeConfig | None = None,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config or AtagiaBridgeConfig.from_env()
        self._client_factory = client_factory or _default_client_factory
        self._client: AtagiaClientProtocol | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def ensure_user_and_conversation(
        self,
        user_id: int | str,
        conversation_id: int | str,
    ) -> str | None:
        """Ensure Atagia resources exist, returning the Atagia conversation id."""
        if not self.enabled:
            return None
        try:
            client = await self._ensure_client()
            atagia_user_id = _to_atagia_id(user_id)
            atagia_conversation_id = _to_atagia_id(conversation_id)
            await client.create_user(atagia_user_id)
            return await client.create_conversation(
                user_id=atagia_user_id,
                conversation_id=atagia_conversation_id,
                assistant_mode_id=self.config.assistant_mode,
            )
        except Exception:
            logger.warning(
                "Atagia ensure_user_and_conversation failed; continuing without sidecar memory",
                exc_info=True,
            )
            return None

    async def get_context_for_turn(
        self,
        user_id: int | str,
        conversation_id: int | str,
        message_text: str,
        *,
        occurred_at: str | None = None,
    ) -> Any | None:
        """Return Atagia context for one user turn, or None on disabled/error."""
        if not self.enabled:
            return None
        if not message_text:
            return None
        try:
            client = await self._ensure_client()
            return await client.get_context(
                user_id=_to_atagia_id(user_id),
                conversation_id=_to_atagia_id(conversation_id),
                message=message_text,
                mode=self.config.assistant_mode,
                occurred_at=occurred_at,
            )
        except Exception:
            logger.warning(
                "Atagia get_context failed; falling back to Aurvek context",
                exc_info=True,
            )
            return None

    async def record_assistant_response(
        self,
        user_id: int | str,
        conversation_id: int | str,
        response_text: str,
        *,
        occurred_at: str | None = None,
    ) -> bool:
        """Persist the assistant response in Atagia, returning success."""
        if not self.enabled:
            return False
        if not response_text:
            return False
        try:
            client = await self._ensure_client()
            await client.add_response(
                user_id=_to_atagia_id(user_id),
                conversation_id=_to_atagia_id(conversation_id),
                text=response_text,
                occurred_at=occurred_at,
            )
            return True
        except Exception:
            logger.warning(
                "Atagia add_response failed; continuing without sidecar persistence",
                exc_info=True,
            )
            return False

    async def close(self) -> None:
        if self._client is None:
            return
        await self._client.close()
        self._client = None

    async def _ensure_client(self) -> AtagiaClientProtocol:
        if self._client is None:
            self._client = await self._client_factory(
                transport=self.config.transport,
                db_path=self.config.db_path,
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout_seconds,
            )
        return self._client


_default_bridge: AtagiaBridge | None = None


def get_atagia_bridge() -> AtagiaBridge:
    """Return the process-wide Atagia bridge singleton."""
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = AtagiaBridge()
    return _default_bridge


async def get_context_for_turn(
    user_id: int | str,
    conversation_id: int | str,
    message_text: str,
    *,
    occurred_at: str | None = None,
) -> Any | None:
    """Module-level convenience wrapper for chat integration."""
    return await get_atagia_bridge().get_context_for_turn(
        user_id,
        conversation_id,
        message_text,
        occurred_at=occurred_at,
    )


async def record_assistant_response(
    user_id: int | str,
    conversation_id: int | str,
    response_text: str,
    *,
    occurred_at: str | None = None,
) -> bool:
    """Module-level convenience wrapper for response persistence."""
    return await get_atagia_bridge().record_assistant_response(
        user_id,
        conversation_id,
        response_text,
        occurred_at=occurred_at,
    )


async def close_atagia_bridge() -> None:
    """Close the process-wide bridge client if it has been initialized."""
    if _default_bridge is not None:
        await _default_bridge.close()


async def _default_client_factory(**kwargs: Any) -> AtagiaClientProtocol:
    from atagia.client import connect_atagia

    return await connect_atagia(**kwargs)


def _parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _parse_transport(value: str | None) -> TransportName:
    transport = (value or "auto").strip().lower()
    if transport not in _VALID_TRANSPORTS:
        logger.warning("Unknown ATAGIA_TRANSPORT=%r; using auto", value)
        return "auto"
    return transport  # type: ignore[return-value]


def _parse_timeout(value: str | None) -> float:
    if value is None or not value.strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except ValueError:
        logger.warning("Invalid ATAGIA_TIMEOUT_SECONDS=%r; using default", value)
        return DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        logger.warning("Invalid ATAGIA_TIMEOUT_SECONDS=%r; using default", value)
        return DEFAULT_TIMEOUT_SECONDS
    return timeout


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _to_atagia_id(value: int | str) -> str:
    return str(value)
