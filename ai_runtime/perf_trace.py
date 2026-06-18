import os
import time
import uuid
from typing import Any

import orjson

from log_config import logger


TRUE_VALUES = {"1", "true", "yes", "on"}


class ChatPerfTrace:
    def __init__(
        self,
        *,
        enabled: bool = False,
        trace_id: str | None = None,
        client_sent_at: str | None = None,
    ):
        self.enabled = enabled
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self.client_sent_at = client_sent_at
        self.started_at = time.perf_counter()
        self._events: list[dict[str, Any]] = []

    @classmethod
    def from_request(cls, request: Any | None) -> "ChatPerfTrace":
        env_enabled = os.getenv("CHAT_PERF_TRACE", "").strip().lower() in TRUE_VALUES
        header_enabled = False
        trace_id = None
        client_sent_at = None
        if request is not None:
            headers = getattr(request, "headers", {}) or {}
            header_enabled = str(headers.get("X-Chat-Trace", "")).strip().lower() in TRUE_VALUES
            trace_id = headers.get("X-Chat-Trace-Id") or None
            client_sent_at = headers.get("X-Chat-Client-Sent-At") or None
        return cls(
            enabled=env_enabled or header_enabled,
            trace_id=trace_id,
            client_sent_at=client_sent_at,
        )

    def mark(self, name: str, **fields: Any) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        event = {
            "type": "perf_trace",
            "trace_id": self.trace_id,
            "name": name,
            "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000, 1),
        }
        if self.client_sent_at is not None:
            event["client_sent_at"] = self.client_sent_at
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                event[key] = value
            else:
                event[key] = str(value)
        self._events.append(event)
        detail = " ".join(
            f"{key}={value}"
            for key, value in event.items()
            if key not in {"type", "trace_id", "name", "client_sent_at"}
        )
        logger.info("[chat_perf] trace_id=%s name=%s %s", self.trace_id, name, detail)
        return event

    def sse(self, name: str, **fields: Any) -> str | None:
        event = self.mark(name, **fields)
        if event is None:
            return None
        return f"data: {orjson.dumps(event).decode()}\n\n"

    def pop_sse(self) -> list[str]:
        if not self.enabled or not self._events:
            return []
        events = self._events
        self._events = []
        return [f"data: {orjson.dumps(event).decode()}\n\n" for event in events]
