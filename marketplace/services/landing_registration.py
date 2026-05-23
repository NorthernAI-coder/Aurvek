import orjson
from fastapi import HTTPException

from common import MAX_FREE_INITIAL_BALANCE
from database import get_db_connection
from log_config import logger


DEFAULT_LANDING_REGISTRATION_CONFIG = {
    "default_llm_id": None,
    "public_prompts_access": True,
    "allow_file_upload": False,
    "allow_image_generation": False,
    "initial_balance": 0.0,
    "billing_mode": "customer_pays",
    "billing_limit": None,
    "billing_limit_action": "block",
    "billing_auto_refill_amount": 10.0,
    "billing_max_limit": None,
    "category_access": None,
}


async def get_landing_registration_config(prompt_id: int) -> dict:
    """
    Get the landing registration configuration for a prompt.
    Returns merged config with defaults for any missing keys.
    """
    async with get_db_connection(readonly=True) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT landing_registration_config, forced_llm_id FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            result = await cursor.fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Prompt not found")

    config_json = result[0]
    forced_llm_id = result[1]

    config = DEFAULT_LANDING_REGISTRATION_CONFIG.copy()

    if config_json:
        try:
            stored_config = orjson.loads(config_json)
            config.update(stored_config)
        except orjson.JSONDecodeError:
            logger.warning(f"Invalid JSON in landing_registration_config for prompt {prompt_id}")

    config["_prompt_forced_llm_id"] = forced_llm_id
    return config


def sanitize_landing_reg_config(
    config: dict,
    max_initial_balance: float = MAX_FREE_INITIAL_BALANCE,
) -> dict:
    """
    Sanitize and validate a landing_reg_config dict.
    Used by both prompts and packs.
    """
    sanitized = {}

    if "default_llm_id" in config:
        val = config["default_llm_id"]
        sanitized["default_llm_id"] = int(val) if val not in (None, "", "null") else None

    for field in ["public_prompts_access", "allow_file_upload", "allow_image_generation"]:
        if field in config:
            sanitized[field] = bool(config[field])

    if "initial_balance" in config:
        try:
            val = float(config.get("initial_balance", 0))
        except (ValueError, TypeError):
            val = 0.0
        sanitized["initial_balance"] = round(min(max(0.0, val), max_initial_balance), 2)

    if "billing_mode" in config:
        val = config["billing_mode"]
        if val in ("customer_pays", "user_pays"):
            sanitized["billing_mode"] = val

    if "billing_limit" in config:
        val = config["billing_limit"]
        if val in (None, "", "null"):
            sanitized["billing_limit"] = None
        else:
            sanitized["billing_limit"] = max(0.0, float(val))

    if "billing_limit_action" in config:
        val = config["billing_limit_action"]
        if val in ("block", "notify", "auto_refill"):
            sanitized["billing_limit_action"] = val

    if "billing_auto_refill_amount" in config:
        val = config["billing_auto_refill_amount"]
        if val not in (None, "", "null"):
            sanitized["billing_auto_refill_amount"] = max(1.0, float(val))

    if "billing_max_limit" in config:
        val = config["billing_max_limit"]
        if val in (None, "", "null"):
            sanitized["billing_max_limit"] = None
        else:
            sanitized["billing_max_limit"] = max(0.0, float(val))

    if "category_access" in config:
        val = config["category_access"]
        if val in (None, "", "null"):
            sanitized["category_access"] = None
        elif isinstance(val, list):
            sanitized["category_access"] = [int(x) for x in val]
        elif isinstance(val, str):
            try:
                parsed = orjson.loads(val)
                if isinstance(parsed, list):
                    sanitized["category_access"] = [int(x) for x in parsed]
            except (orjson.JSONDecodeError, ValueError):
                pass

    return sanitized


async def set_landing_registration_config(prompt_id: int, config: dict) -> bool:
    """
    Set the landing registration configuration for a prompt.
    Returns True on success, False on failure.
    """
    sanitized = sanitize_landing_reg_config(
        config,
        max_initial_balance=MAX_FREE_INITIAL_BALANCE,
    )

    try:
        config_json = orjson.dumps(sanitized).decode("utf-8")

        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE PROMPTS SET landing_registration_config = ? WHERE id = ?",
                (config_json, prompt_id),
            )
            await conn.commit()

        return True
    except Exception as e:
        logger.error(f"Error setting landing_registration_config for prompt {prompt_id}: {e}")
        return False
