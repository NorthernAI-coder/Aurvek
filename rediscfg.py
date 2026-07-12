# rediscfg.py

# Modified to initialize Redis when the application starts and avoid the 2 second delay to connect to the pool


import asyncio
import math
import os
import secrets
import time
from collections import OrderedDict, deque
import dramatiq
from datetime import timedelta
from typing import Optional
from redis import asyncio as aioredis
from dramatiq.brokers.redis import RedisBroker
from log_config import logger


_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local member = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
if count >= limit then
    redis.call('EXPIRE', key, ttl)
    return {0, count}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl)
return {1, count + 1}
"""

_RATE_LIMIT_STATUS_SCRIPT = """
local key = KEYS[1]
local cutoff = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
if count > 0 then
    redis.call('EXPIRE', key, ttl)
end
if #oldest == 0 then
    return {count, ''}
end
return {count, oldest[2]}
"""

_LOCAL_RATE_LIMIT_MAX_KEYS = 10_000
_local_rate_limit_buckets: OrderedDict[str, deque[float]] = OrderedDict()
_local_rate_limit_lock = asyncio.Lock()

class RedisManager:
    _instance = None
    _sync_pool = None
    _async_pool = None
    _sync_client = None
    _async_client = None
    _broker = None

    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        if self._sync_pool is None:
            self._sync_pool = aioredis.ConnectionPool(
                host=self.REDIS_HOST,
                port=self.REDIS_PORT,
                db=self.REDIS_DB,
                decode_responses=True,
                max_connections=30,
                health_check_interval=45,
                socket_keepalive=True,
                socket_timeout=300,
                retry_on_timeout=True
            )

        if self._async_pool is None:
            self._async_pool = aioredis.ConnectionPool(
                host=self.REDIS_HOST,
                port=self.REDIS_PORT,
                db=self.REDIS_DB,
                decode_responses=True,
                max_connections=30
            )

        if self._broker is None:
            self._broker = RedisBroker(host=self.REDIS_HOST, port=self.REDIS_PORT)
            # Add middleware
            self._broker.add_middleware(dramatiq.middleware.AgeLimit(max_age=300000))  # 5 minutes
            self._broker.add_middleware(dramatiq.middleware.TimeLimit(time_limit=600000))  # 10 minutes
            self._broker.add_middleware(dramatiq.middleware.Retries(max_retries=0))  # No retries
            dramatiq.set_broker(self._broker)

    def get_sync_client(self) -> aioredis.Redis:
        if self._sync_client is None:
            self._sync_client = aioredis.Redis(connection_pool=self._sync_pool)
        return self._sync_client

    def get_async_client(self) -> aioredis.Redis:
        if self._async_client is None:
            self._async_client = aioredis.Redis(connection_pool=self._async_pool)
        return self._async_client

    def get_broker(self) -> RedisBroker:
        return self._broker

    @classmethod
    async def close(cls):
        if cls._instance:
            if cls._instance._async_client:
                await cls._instance._async_client.close()
            if cls._instance._sync_client:
                await cls._instance._sync_client.close()
            if cls._instance._sync_pool:
                await cls._instance._sync_pool.disconnect()
            if cls._instance._async_pool:
                await cls._instance._async_pool.disconnect()
            if cls._instance._broker:
                cls._instance._broker.shutdown()
            cls._instance = None

# Get the manager instance
redis_manager = RedisManager.get_instance()

# Get the clients and broker
redis_client = redis_manager.get_async_client()
broker = redis_manager.get_broker()

async def add_revoked_user(user_id: int, ttl: Optional[timedelta] = timedelta(hours=4)):
    try:
        key = f"revoked_user:{user_id}"
        if ttl is None:
            await redis_client.set(key, 1)
        else:
            await redis_client.setex(key, ttl, 1)
        return True
    except Exception as e:
        logger.error(f"Error adding revoked user to Redis: {e}")
        return False

async def remove_revoked_user(user_id: int):
    try:
        await redis_client.delete(f"revoked_user:{user_id}")
        return True
    except Exception as e:
        logger.error(f"Error removing revoked user from Redis: {e}")
        return False

async def is_user_revoked(user_id: int) -> bool:
    try:
        exists = await redis_client.exists(f"revoked_user:{user_id}")
        return bool(exists)
    except Exception as e:
        logger.error(f"Error checking revoked user in Redis: {e}")
        return False

async def close_redis_connection():
    await RedisManager.close()

# Rate limiting functions
def _rate_limit_parameters(limit: int, window_minutes: int) -> tuple[int, float, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    if isinstance(window_minutes, bool) or not isinstance(window_minutes, (int, float)) or window_minutes <= 0:
        raise ValueError("window_minutes must be positive")

    window_seconds = float(window_minutes) * 60
    ttl_seconds = max(1, math.ceil(window_seconds + 60))
    return limit, window_seconds, ttl_seconds


def _prune_local_bucket(bucket: deque[float], cutoff: float) -> None:
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def _get_local_bucket(key: str) -> deque[float]:
    bucket = _local_rate_limit_buckets.get(key)
    if bucket is None:
        if len(_local_rate_limit_buckets) >= _LOCAL_RATE_LIMIT_MAX_KEYS:
            _local_rate_limit_buckets.popitem(last=False)
        bucket = deque()
        _local_rate_limit_buckets[key] = bucket
    else:
        _local_rate_limit_buckets.move_to_end(key)
    return bucket


async def _record_local_rate_limit(
    key: str,
    *,
    limit: int,
    window_seconds: float,
    now: float,
    enforce_limit: bool,
) -> tuple[bool, int, Optional[float]]:
    async with _local_rate_limit_lock:
        bucket = _get_local_bucket(key)
        _prune_local_bucket(bucket, now - window_seconds)
        if enforce_limit and len(bucket) >= limit:
            oldest = bucket[0] if bucket else None
            return False, len(bucket), oldest
        bucket.append(now)
        return True, len(bucket), bucket[0]


async def _get_local_rate_limit_status(
    key: str,
    *,
    window_seconds: float,
    now: float,
) -> tuple[int, Optional[float]]:
    async with _local_rate_limit_lock:
        bucket = _get_local_bucket(key)
        _prune_local_bucket(bucket, now - window_seconds)
        if not bucket:
            _local_rate_limit_buckets.pop(key, None)
            return 0, None
        return len(bucket), bucket[0]


async def check_rate_limit(user_id: int, action: str = "ai_call", limit: int = 30, window_minutes: int = 1) -> bool:
    """
    Check if user has exceeded rate limit for a specific action.
    Uses sliding window counter with Redis.

    Args:
        user_id: User ID to check
        action: Action type (default: 'ai_call')
        limit: Max requests per window (default: 30)
        window_minutes: Time window in minutes (default: 1)

    Returns:
        True if under limit, False if exceeded
    """
    limit, window_seconds, ttl_seconds = _rate_limit_parameters(limit, window_minutes)
    current_time = time.time()
    window_start = current_time - window_seconds
    key = f"rate_limit:{action}:{user_id}"
    member = f"{time.time_ns()}:{secrets.token_hex(8)}"

    try:
        result = await redis_client.eval(
            _RATE_LIMIT_SCRIPT,
            1,
            key,
            current_time,
            window_start,
            limit,
            ttl_seconds,
            member,
        )
        allowed = bool(int(result[0]))
        current_count = int(result[1])
        if allowed:
            # Mirror accepted requests so a Redis outage degrades to a bounded,
            # process-local limiter instead of suddenly failing open.
            await _record_local_rate_limit(
                key,
                limit=limit,
                window_seconds=window_seconds,
                now=current_time,
                enforce_limit=False,
            )
        else:
            logger.warning(
                "Rate limit exceeded for user %s, action %s: %s/%s",
                user_id,
                action,
                current_count,
                limit,
            )
        return allowed
    except Exception as e:
        logger.error(f"Error checking rate limit for user {user_id}: {e}")
        allowed, current_count, _ = await _record_local_rate_limit(
            key,
            limit=limit,
            window_seconds=window_seconds,
            now=current_time,
            enforce_limit=True,
        )
        if not allowed:
            logger.warning(
                "Local rate limit exceeded for user %s, action %s: %s/%s",
                user_id,
                action,
                current_count,
                limit,
            )
        return allowed

async def get_rate_limit_status(user_id: int, action: str = "ai_call", limit: int = 30, window_minutes: int = 1) -> dict:
    """
    Get current rate limit status for user.

    Returns:
        Dict with current count, limit, and reset time
    """
    limit, window_seconds, ttl_seconds = _rate_limit_parameters(limit, window_minutes)
    current_time = time.time()
    key = f"rate_limit:{action}:{user_id}"

    try:
        result = await redis_client.eval(
            _RATE_LIMIT_STATUS_SCRIPT,
            1,
            key,
            current_time - window_seconds,
            ttl_seconds,
        )
        current_count = int(result[0])
        oldest_timestamp = float(result[1]) if result[1] not in (None, "", b"") else None
    except Exception as e:
        logger.error(f"Error getting rate limit status for user {user_id}: {e}")
        current_count, oldest_timestamp = await _get_local_rate_limit_status(
            key,
            window_seconds=window_seconds,
            now=current_time,
        )

    reset_time = None
    if oldest_timestamp is not None:
        reset_time = math.ceil(oldest_timestamp + window_seconds)

    return {
        "current": current_count,
        "limit": limit,
        "remaining": max(0, limit - current_count),
        "reset_time": reset_time,
        "window_minutes": window_minutes,
    }

# Basic metrics functions
async def increment_metric(metric_name: str, value: int = 1, ttl_hours: int = 24):
    """
    Increment a metric counter in Redis.
    
    Args:
        metric_name: Name of the metric (e.g., 'api_calls', 'ai_requests', 'users_active')
        value: Value to increment by (default: 1)
        ttl_hours: Hours to keep the metric (default: 24)
    """
    try:
        key = f"metrics:{metric_name}"
        await redis_client.incrby(key, value)
        await redis_client.expire(key, ttl_hours * 3600)
    except Exception as e:
        logger.error(f"Error incrementing metric {metric_name}: {e}")

async def get_metrics() -> dict:
    """
    Get all current metrics from Redis.
    
    Returns:
        Dict with metric names and their current values
    """
    try:
        # Get all metric keys
        metric_keys = await redis_client.keys("metrics:*")
        metrics = {}
        
        if metric_keys:
            # Get all values in one call
            values = await redis_client.mget(metric_keys)
            
            for key, value in zip(metric_keys, values):
                # Remove 'metrics:' prefix from key name
                clean_key = key.replace("metrics:", "")
                metrics[clean_key] = int(value) if value else 0
        
        return metrics
        
    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        return {}

async def increment_user_activity(user_id: int):
    """
    Track active users for basic analytics.
    Uses a set to count unique active users per hour.
    """
    try:
        import time
        current_hour = int(time.time() // 3600)  # Current hour as timestamp
        key = f"metrics:active_users:{current_hour}"
        
        await redis_client.sadd(key, user_id)
        await redis_client.expire(key, 7200)  # Keep for 2 hours
        
    except Exception as e:
        logger.error(f"Error tracking user activity for user {user_id}: {e}")

async def get_active_users_count() -> int:
    """
    Get count of active users in current hour.
    """
    try:
        import time
        current_hour = int(time.time() // 3600)
        key = f"metrics:active_users:{current_hour}"
        
        count = await redis_client.scard(key)
        return count
        
    except Exception as e:
        logger.error(f"Error getting active users count: {e}")
        return 0

async def close_redis_connection():
    await RedisManager.close()

# Export broker and Redis client for use in other files
__all__ = ['broker', 'redis_client', 'add_revoked_user', 'remove_revoked_user', 'is_user_revoked', 'close_redis_connection', 'RedisManager', 'check_rate_limit', 'get_rate_limit_status', 'increment_metric', 'get_metrics', 'increment_user_activity', 'get_active_users_count']
