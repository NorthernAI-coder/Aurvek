from ai_runtime.dependencies import *

ATAGIA_LIVE_INGEST_ORIGIN = "live_turn"
ATAGIA_LIVE_CONFIRMATION_STRATEGY = "live_prompt_allowed"
_current_atagia_user_message_id: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("current_atagia_user_message_id", default=None)
)
