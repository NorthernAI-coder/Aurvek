# gransabio_config.py
# Configuration management for GranSabio integration.
# Handles URL validation (SSRF protection), config caching, and model pricing.

import ipaddress
import os
import time
from urllib.parse import urlparse
from typing import Optional

import orjson

from database import get_db_connection
from log_config import logger

# ---------------------------------------------------------------------------
# Deployment config (env-based, read once at import)
# ---------------------------------------------------------------------------

GRANSABIO_USE_DRAMATIQ = os.getenv("GRANSABIO_USE_DRAMATIQ", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Defaults (used when SYSTEM_CONFIG keys are missing)
# ---------------------------------------------------------------------------

GRANSABIO_DEFAULTS = {
    "gransabio_enabled": "false",
    "gransabio_url": "http://127.0.0.1:8000",
    "gransabio_default_generator": "",
    "gransabio_default_qa_models": "[]",
    "gransabio_default_min_score": "8.0",
    "gransabio_default_max_iterations": "3",
    "gransabio_default_gran_sabio_model": "",
    "gransabio_default_arbiter_model": "",
    "gransabio_default_smart_edit": "auto",
    "gransabio_default_gran_sabio_fallback": "true",
    "gransabio_default_verbose": "false",
    "gransabio_default_context_max_tokens": "4000",
    "gransabio_cost_safety_multiplier": "3",
    "gransabio_extra_allowed_ips": "",
}

# ---------------------------------------------------------------------------
# IP allowlist for SSRF protection
# ---------------------------------------------------------------------------

# Default: localhost only. LAN hosts added via gransabio_extra_allowed_ips.
ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.1/32"),
    ipaddress.ip_network("::1/128"),
]


def validate_extra_allowed_ips(raw: str) -> list[str]:
    """Validate comma-separated CIDRs. Only single-host (/32 or /128) accepted.

    Design decision: subnets are intentionally rejected. There won't be many
    GranSabio instances, so specifying each IP exactly is trivial and avoids
    accidental broad SSRF allowlists (e.g. /16 by typo). The proposal mentions
    subnet defaults on the GranSabio side, but Aurvek's SSRF validation is
    strict by choice.
    """
    validated = []
    for cidr_str in raw.split(","):
        cidr_str = cidr_str.strip()
        if not cidr_str:
            continue
        net = ipaddress.ip_network(cidr_str, strict=False)
        if isinstance(net, ipaddress.IPv4Network) and net.prefixlen < 32:
            raise ValueError(
                f"Rejected broad IPv4 range: {cidr_str} (prefix /{net.prefixlen}). "
                f"Only /32 (single host) allowed. Did you mean {net.network_address}/32?"
            )
        if isinstance(net, ipaddress.IPv6Network) and net.prefixlen < 128:
            raise ValueError(
                f"Rejected broad IPv6 range: {cidr_str} (prefix /{net.prefixlen}). "
                f"Only /128 (single host) allowed. Did you mean {net.network_address}/128?"
            )
        validated.append(str(net))
    return validated


def validate_gransabio_url(url: str, extra_allowed_ips_raw: str = "") -> tuple[bool, str]:
    """Strict SSRF-safe URL validation. Only IP literals in allowlist accepted.

    Returns (ok, error_message).
    """
    if not url:
        return False, "URL is required."

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "Only http:// and https:// schemes are allowed."

    host = parsed.hostname
    if not host:
        return False, "URL must include a host."

    # Must be an IP literal (no DNS resolution = no rebinding)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, f"Host must be an IP literal, not a hostname ('{host}'). DNS-based names are rejected for SSRF protection."

    # Build full allowlist: built-in + extra from SYSTEM_CONFIG
    allowed = list(ALLOWED_NETWORKS)
    if extra_allowed_ips_raw:
        try:
            extra = validate_extra_allowed_ips(extra_allowed_ips_raw)
            allowed.extend(ipaddress.ip_network(n) for n in extra)
        except ValueError as e:
            return False, f"Invalid extra allowed IPs: {e}"

    if not ip.is_private and not any(ip in net for net in allowed):
        return False, f"IP {ip} is not in the allowed network list."

    # Public IPs require HTTPS; private/loopback can use HTTP (internal traffic)
    if not ip.is_loopback and not ip.is_private and parsed.scheme != "https":
        return False, f"Public IPs require https:// (got http:// for {ip})."

    return True, ""


# ---------------------------------------------------------------------------
# Config cache (same pattern as tts_config.py)
# ---------------------------------------------------------------------------

_config_cache: Optional[dict] = None
_config_cache_time: float = 0
CONFIG_CACHE_TTL = 300  # 5 minutes


async def get_gransabio_config() -> dict:
    """Load gransabio_* keys from SYSTEM_CONFIG, merge with defaults. Cached."""
    global _config_cache, _config_cache_time

    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < CONFIG_CACHE_TTL:
        return _config_cache

    config = dict(GRANSABIO_DEFAULTS)
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'gransabio_%'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                config[row[0]] = row[1]
    except Exception as e:
        logger.error(f"Failed to load GranSabio config from DB: {e}")
        # Fall through with defaults

    _config_cache = config
    _config_cache_time = now
    return config


def invalidate_gransabio_config_cache():
    """Called after admin saves config."""
    global _config_cache, _config_cache_time
    _config_cache = None
    _config_cache_time = 0


# ---------------------------------------------------------------------------
# Model pricing cache (in-memory, NOT persisted in SYSTEM_CONFIG)
# ---------------------------------------------------------------------------

_pricing_cache: Optional[dict] = None
_pricing_cache_time: float = 0
_pricing_cache_url: str = ""  # Key cache by URL
PRICING_CACHE_TTL = 300  # 5 minutes


async def get_gransabio_model_pricing(url: str, extra_allowed_ips: str = "") -> dict:
    """Fetch model pricing from GranSabio /models endpoint. Cached in-memory, keyed by URL.

    Returns dict mapping model_id -> {"input_cost_per_token": float, "output_cost_per_token": float}.
    Returns empty dict on failure (caller must handle missing pricing).
    """
    global _pricing_cache, _pricing_cache_time, _pricing_cache_url

    now = time.time()
    if (_pricing_cache is not None
            and (now - _pricing_cache_time) < PRICING_CACHE_TTL
            and _pricing_cache_url == url):
        return _pricing_cache

    # SSRF validation before any HTTP call (include extra IPs from admin config)
    ok, err = validate_gransabio_url(url, extra_allowed_ips)
    if not ok:
        logger.warning("get_gransabio_model_pricing: URL rejected: %s", err)
        return _pricing_cache or {}

    try:
        import httpx
        # Use a local client (not the module-level cached one from gransabio_service)
        # because this function may be called from Dramatiq workers with a different event loop
        async with httpx.AsyncClient(timeout=10.0, trust_env=False, follow_redirects=False) as client:
            resp = await client.get(f"{url}/models")
        resp.raise_for_status()
        data = resp.json()

        pricing = {}
        models = data if isinstance(data, list) else data.get("models", [])
        for model in models:
            model_id = model.get("id") or model.get("model_id") or model.get("name")
            if not model_id:
                continue
            input_cost = model.get("input_cost_per_token", 0)
            output_cost = model.get("output_cost_per_token", 0)
            pricing[model_id] = {
                "input_cost_per_token": float(input_cost),
                "output_cost_per_token": float(output_cost),
            }

        _pricing_cache = pricing
        _pricing_cache_time = now
        _pricing_cache_url = url
        return pricing

    except Exception as e:
        logger.warning(f"Failed to fetch GranSabio model pricing: {e}")
        if _pricing_cache is not None:
            return _pricing_cache
        return {}


def invalidate_pricing_cache():
    """Force refresh on next call."""
    global _pricing_cache, _pricing_cache_time
    _pricing_cache = None
    _pricing_cache_time = 0
