from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import RLock
import time
from typing import Any


MEMORY_OPERATIONAL = "operational"
MEMORY_SUSPECTED = "suspected"
MEMORY_DEGRADED = "degraded"
MEMORY_UNAVAILABLE = "unavailable"
MEMORY_RECOVERING = "recovering"
MEMORY_DISABLED = "disabled"

SURFACED_MEMORY_STATUSES = {
    MEMORY_SUSPECTED,
    MEMORY_DEGRADED,
    MEMORY_UNAVAILABLE,
}

_MAX_EVENTS_PER_PROVIDER = 200
_EVENT_TTL_SECONDS = 15 * 60
_RECOVERY_SUCCESS_THRESHOLD = 3

_PROVIDER_DISPLAY_NAMES = {
    "atagia": "Atagia",
    "mem0": "Mem0",
    "none": "Local context",
}


@dataclass(slots=True)
class MemoryHealthEvent:
    timestamp: float
    kind: str
    operation: str
    status: str
    latency_ms: float | None = None
    error_type: str = ""
    message: str = ""


@dataclass(slots=True)
class MemoryHealthState:
    provider: str
    status: str = MEMORY_OPERATIONAL
    source: str = "local"
    message: str = ""
    last_operation: str = ""
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_latency_ms: float | None = None
    last_error_type: str = ""
    last_error_message: str = ""
    success_streak: int = 0
    last_status_change_at: float = field(default_factory=time.time)
    events: deque[MemoryHealthEvent] = field(
        default_factory=lambda: deque(maxlen=_MAX_EVENTS_PER_PROVIDER)
    )


_state_lock = RLock()
_states: dict[str, MemoryHealthState] = {}


def normalize_memory_provider(value: str | None) -> str:
    provider = (value or "").strip().lower()
    if provider in {"atagia", "mem0", "none"}:
        return provider
    return provider or "none"


def memory_provider_display_name(provider: str | None) -> str:
    key = normalize_memory_provider(provider)
    return _PROVIDER_DISPLAY_NAMES.get(key, key or "Memory")


def should_surface_memory_health(health: dict[str, Any] | None) -> bool:
    if not health:
        return False
    return bool(health.get("enabled", True)) and health.get("status") in SURFACED_MEMORY_STATUSES


def record_memory_success(
    provider: str | None,
    operation: str,
    *,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    provider_key = normalize_memory_provider(provider)
    if provider_key == "none":
        return _disabled_snapshot()

    now = time.time()
    with _state_lock:
        state = _get_state_locked(provider_key)
        _prune_events_locked(state, now)
        state.last_operation = operation
        state.last_success_at = now
        state.last_latency_ms = latency_ms
        state.last_error_type = ""
        state.last_error_message = ""
        state.success_streak += 1

        if state.status in {MEMORY_SUSPECTED, MEMORY_DEGRADED, MEMORY_UNAVAILABLE}:
            _set_status_locked(state, MEMORY_RECOVERING, "runtime_success")
            state.message = _recovering_admin_message(state)
        elif (
            state.status == MEMORY_RECOVERING
            and state.success_streak >= _RECOVERY_SUCCESS_THRESHOLD
        ):
            _set_status_locked(state, MEMORY_OPERATIONAL, "runtime_success")
            state.message = ""
        elif state.status == MEMORY_OPERATIONAL:
            state.source = "runtime_success"
            state.message = ""

        state.events.append(
            MemoryHealthEvent(
                timestamp=now,
                kind="success",
                operation=operation,
                status=state.status,
                latency_ms=latency_ms,
            )
        )
        return _admin_snapshot_locked(state)


def record_memory_failure(
    provider: str | None,
    operation: str,
    *,
    message: str | None = None,
    exception: BaseException | None = None,
    latency_ms: float | None = None,
    unavailable: bool = False,
) -> dict[str, Any]:
    provider_key = normalize_memory_provider(provider)
    if provider_key == "none":
        return _disabled_snapshot()

    now = time.time()
    error_type = exception.__class__.__name__ if exception is not None else ""
    error_message = _clean_error_message(message or (str(exception) if exception else ""))
    status = MEMORY_UNAVAILABLE if unavailable or operation == "test_connection" else MEMORY_DEGRADED

    with _state_lock:
        state = _get_state_locked(provider_key)
        _prune_events_locked(state, now)
        state.last_operation = operation
        state.last_failure_at = now
        state.last_latency_ms = latency_ms
        state.last_error_type = error_type
        state.last_error_message = error_message
        state.success_streak = 0
        _set_status_locked(state, status, "runtime_error")
        state.message = _failure_admin_message(state)
        state.events.append(
            MemoryHealthEvent(
                timestamp=now,
                kind="failure",
                operation=operation,
                status=status,
                latency_ms=latency_ms,
                error_type=error_type,
                message=error_message,
            )
        )
        return _admin_snapshot_locked(state)


def get_memory_health(provider: str | None) -> dict[str, Any]:
    provider_key = normalize_memory_provider(provider)
    if provider_key == "none":
        return _disabled_snapshot()
    now = time.time()
    with _state_lock:
        state = _get_state_locked(provider_key)
        _prune_events_locked(state, now)
        return _admin_snapshot_locked(state)


def get_user_memory_health_snapshot(
    provider: str | None,
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    provider_key = normalize_memory_provider(provider)
    if provider_key == "none" or not enabled:
        return _user_disabled_snapshot()

    health = get_memory_health(provider_key)
    status = str(health.get("status") or MEMORY_OPERATIONAL)
    should_surface = status in SURFACED_MEMORY_STATUSES
    return {
        "enabled": True,
        "status": status,
        "should_surface": should_surface,
        "message": _user_message_for_status(status) if should_surface else "",
        "last_checked_at": health.get("last_activity_at"),
        "last_success_at": health.get("last_success_at"),
        "last_failure_at": health.get("last_failure_at"),
    }


def get_admin_memory_health_snapshot(provider: str | None) -> dict[str, Any]:
    provider_key = normalize_memory_provider(provider)
    if provider_key == "none":
        return _disabled_snapshot()
    return get_memory_health(provider_key)


def reset_memory_health_state() -> None:
    with _state_lock:
        _states.clear()


def _get_state_locked(provider: str) -> MemoryHealthState:
    state = _states.get(provider)
    if state is None:
        state = MemoryHealthState(provider=provider)
        _states[provider] = state
    return state


def _set_status_locked(state: MemoryHealthState, status: str, source: str) -> None:
    if state.status != status:
        state.status = status
        state.last_status_change_at = time.time()
    state.source = source


def _prune_events_locked(state: MemoryHealthState, now: float) -> None:
    cutoff = now - _EVENT_TTL_SECONDS
    while state.events and state.events[0].timestamp < cutoff:
        state.events.popleft()


def _admin_snapshot_locked(state: MemoryHealthState) -> dict[str, Any]:
    last_activity_at = max(
        value
        for value in (
            state.last_success_at or 0,
            state.last_failure_at or 0,
            state.last_status_change_at or 0,
        )
    )
    return {
        "provider": state.provider,
        "provider_name": memory_provider_display_name(state.provider),
        "enabled": True,
        "status": state.status,
        "source": state.source,
        "message": state.message,
        "should_surface": state.status in SURFACED_MEMORY_STATUSES,
        "last_operation": state.last_operation,
        "last_success_at": state.last_success_at,
        "last_failure_at": state.last_failure_at,
        "last_activity_at": last_activity_at or None,
        "last_latency_ms": state.last_latency_ms,
        "last_error_type": state.last_error_type,
        "last_error_message": state.last_error_message,
        "success_streak": state.success_streak,
        "recent_events": [
            {
                "timestamp": event.timestamp,
                "kind": event.kind,
                "operation": event.operation,
                "status": event.status,
                "latency_ms": event.latency_ms,
                "error_type": event.error_type,
                "message": event.message,
            }
            for event in list(state.events)[-10:]
        ],
    }


def _disabled_snapshot() -> dict[str, Any]:
    return {
        "provider": "none",
        "provider_name": memory_provider_display_name("none"),
        "enabled": False,
        "status": MEMORY_DISABLED,
        "source": "configured",
        "message": "",
        "should_surface": False,
        "last_operation": "",
        "last_success_at": None,
        "last_failure_at": None,
        "last_activity_at": None,
        "last_latency_ms": None,
        "last_error_type": "",
        "last_error_message": "",
        "success_streak": 0,
        "recent_events": [],
    }


def _user_disabled_snapshot() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": MEMORY_DISABLED,
        "should_surface": False,
        "message": "",
        "last_checked_at": None,
        "last_success_at": None,
        "last_failure_at": None,
    }


def _failure_admin_message(state: MemoryHealthState) -> str:
    name = memory_provider_display_name(state.provider)
    if state.status == MEMORY_UNAVAILABLE:
        return f"{name} is not available. Aurvek will fall back to local context."
    return f"{name} memory is degraded. Aurvek will fall back to local context when needed."


def _recovering_admin_message(state: MemoryHealthState) -> str:
    name = memory_provider_display_name(state.provider)
    return f"{name} recently failed, but successful memory operations are being seen again."


def _user_message_for_status(status: str) -> str:
    if status == MEMORY_UNAVAILABLE:
        return "Memory is temporarily unavailable. Replies may not use saved long-term memory."
    if status == MEMORY_DEGRADED:
        return "Memory is temporarily degraded. Replies may not use saved long-term memory."
    if status == MEMORY_SUSPECTED:
        return "Memory may be temporarily degraded. Replies may not use saved long-term memory."
    return ""


def _clean_error_message(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    text = " ".join(text.split())
    return text[:500]
