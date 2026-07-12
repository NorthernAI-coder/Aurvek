"""Purpose-bound, rate-limited SMS phone verification.

The module deliberately keeps the HTTP layer and Twilio client construction out
of the service.  Callers provide the authenticated actor, request IP, Twilio
client, and Verify service SID.  Approved challenges are consumed by the final
operation using the caller's existing database transaction.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import aiosqlite

from database import get_db_connection


logger = logging.getLogger(__name__)


class PhoneVerificationPurpose(str, Enum):
    CREATE_USER = "create_user"
    PROFILE_PHONE_CHANGE = "profile_phone_change"


PURPOSE_CREATE_USER = PhoneVerificationPurpose.CREATE_USER.value
PURPOSE_PROFILE_PHONE_CHANGE = PhoneVerificationPurpose.PROFILE_PHONE_CHANGE.value

_ACTIVE_STATUSES = ("reserved", "pending", "approved")
_CHALLENGE_COLUMNS = """
    id, actor_user_id, phone_number, purpose, status,
    verification_attempts, created_at, expires_at, approved_at, consumed_at
"""
_PHONE_ALLOWED_SEPARATORS_RE = re.compile(r"[\s().-]+")
_E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_CODE_RE = re.compile(r"^[0-9]{4,10}$")


class TwilioVerifyClient(Protocol):
    async def send_verification(
        self, service_sid: str, to: str, channel: str = "sms"
    ) -> Mapping[str, Any]: ...

    async def check_verification(
        self, service_sid: str, to: str, code: str
    ) -> Mapping[str, Any]: ...


ConnectionFactory = Callable[
    [], AbstractAsyncContextManager[aiosqlite.Connection]
]


class PhoneVerificationError(Exception):
    """Base error safe to translate directly into an HTTP response."""

    status_code = 400
    code = "phone_verification_error"
    default_detail = "Phone verification could not be completed."

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class InvalidPhoneNumberError(PhoneVerificationError):
    code = "invalid_phone_number"
    default_detail = "Enter a valid phone number in international format."


class InvalidVerificationRequestError(PhoneVerificationError):
    code = "invalid_verification_request"
    default_detail = "The phone verification request is invalid."


class PhoneVerificationNotFoundError(PhoneVerificationError):
    status_code = 404
    code = "phone_verification_not_found"
    default_detail = "The phone verification request was not found."


class PhoneVerificationExpiredError(PhoneVerificationError):
    status_code = 410
    code = "phone_verification_expired"
    default_detail = "The phone verification request has expired."


class PhoneVerificationNotApprovedError(PhoneVerificationError):
    status_code = 409
    code = "phone_verification_not_approved"
    default_detail = "The phone number has not been verified."


class PhoneVerificationAlreadyConsumedError(PhoneVerificationError):
    status_code = 409
    code = "phone_verification_already_used"
    default_detail = "The phone verification request has already been used."


class InvalidVerificationCodeError(PhoneVerificationError):
    code = "invalid_verification_code"
    default_detail = "The verification code is invalid."


class VerificationAttemptsExceededError(PhoneVerificationError):
    status_code = 429
    code = "phone_verification_attempts_exceeded"
    default_detail = "Too many verification attempts. Request a new code."


class PhoneVerificationRateLimitError(PhoneVerificationError):
    status_code = 429
    code = "phone_verification_rate_limited"
    default_detail = "Too many verification requests. Try again later."

    def __init__(self, *, retry_after: int, scope: str):
        self.retry_after = max(1, int(retry_after))
        self.scope = scope
        super().__init__()


class PhoneVerificationServiceUnavailableError(PhoneVerificationError):
    status_code = 503
    code = "phone_verification_unavailable"
    default_detail = "Phone verification is temporarily unavailable."


class PhoneVerificationProviderError(PhoneVerificationError):
    status_code = 502
    code = "phone_verification_provider_error"
    default_detail = "The verification provider could not complete the request."


@dataclass(frozen=True)
class PhoneVerificationLimits:
    cooldown_seconds: int = 60
    actor_hourly_limit: int = 10
    phone_hourly_limit: int = 3
    ip_hourly_limit: int = 20
    global_daily_limit: int = 200
    challenge_ttl_seconds: int = 600
    max_verification_attempts: int = 5
    retention_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        for field_name in (
            "actor_hourly_limit",
            "phone_hourly_limit",
            "ip_hourly_limit",
            "global_daily_limit",
            "challenge_ttl_seconds",
            "max_verification_attempts",
            "retention_seconds",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.max_verification_attempts > 5:
            raise ValueError("max_verification_attempts cannot exceed 5")
        if self.retention_seconds < 86400:
            raise ValueError("retention_seconds must cover the 24-hour global window")
        if self.retention_seconds < self.challenge_ttl_seconds:
            raise ValueError("retention_seconds must cover the challenge lifetime")

    @classmethod
    def from_env(cls) -> "PhoneVerificationLimits":
        challenge_ttl_seconds = _env_int(
            "SMS_VERIFICATION_TTL_SECONDS", 600
        )
        retention_seconds = max(
            86400,
            challenge_ttl_seconds,
            _env_int(
                "SMS_VERIFICATION_RETENTION_SECONDS", 7 * 24 * 60 * 60
            ),
        )
        return cls(
            cooldown_seconds=_env_int(
                "SMS_VERIFICATION_COOLDOWN_SECONDS", 60, minimum=0
            ),
            actor_hourly_limit=_env_int(
                "SMS_VERIFICATION_ACTOR_HOURLY_LIMIT", 10
            ),
            phone_hourly_limit=_env_int(
                "SMS_VERIFICATION_PHONE_HOURLY_LIMIT", 3
            ),
            ip_hourly_limit=_env_int(
                "SMS_VERIFICATION_IP_HOURLY_LIMIT", 20
            ),
            global_daily_limit=_env_int(
                "SMS_VERIFICATION_GLOBAL_DAILY_LIMIT", 200
            ),
            challenge_ttl_seconds=challenge_ttl_seconds,
            max_verification_attempts=min(
                5, _env_int("SMS_VERIFICATION_MAX_ATTEMPTS", 5)
            ),
            retention_seconds=retention_seconds,
        )


@dataclass(frozen=True)
class PhoneVerificationChallenge:
    challenge_id: str
    actor_user_id: int
    phone_number: str
    purpose: str
    status: str
    verification_attempts: int
    created_at: int
    expires_at: int
    approved_at: int | None = None
    consumed_at: int | None = None

    @property
    def expires_in(self) -> int:
        return max(0, self.expires_at - int(time.time()))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid integer configuration for %s", name)
        return default
    if value < minimum:
        logger.warning("Ignoring out-of-range configuration for %s", name)
        return default
    return value


def normalize_phone_number(phone: str) -> str:
    """Return a canonical E.164 number without guessing a country code."""

    if not isinstance(phone, str):
        raise InvalidPhoneNumberError()

    normalized = _PHONE_ALLOWED_SEPARATORS_RE.sub("", phone.strip())
    if normalized.startswith("00"):
        normalized = "+" + normalized[2:]

    if not _E164_RE.fullmatch(normalized):
        raise InvalidPhoneNumberError()
    return normalized


def normalize_verification_purpose(
    purpose: str | PhoneVerificationPurpose,
) -> str:
    try:
        return PhoneVerificationPurpose(purpose).value
    except (TypeError, ValueError) as exc:
        raise InvalidVerificationRequestError() from exc


def _validate_challenge_id(challenge_id: str) -> str:
    if not isinstance(challenge_id, str):
        raise InvalidVerificationRequestError()
    challenge_id = challenge_id.strip()
    if not challenge_id or len(challenge_id) > 200:
        raise InvalidVerificationRequestError()
    return challenge_id


def _validate_verification_code(code: str) -> str:
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code.strip()):
        raise InvalidVerificationCodeError()
    return code.strip()


def _get_connection_context(
    connection_factory: ConnectionFactory | None,
) -> AbstractAsyncContextManager[aiosqlite.Connection]:
    factory = connection_factory or get_db_connection
    return factory()


def _challenge_from_row(row: Any) -> PhoneVerificationChallenge:
    return PhoneVerificationChallenge(
        challenge_id=row[0],
        actor_user_id=int(row[1]),
        phone_number=row[2],
        purpose=row[3],
        status=row[4],
        verification_attempts=int(row[5]),
        created_at=int(row[6]),
        expires_at=int(row[7]),
        approved_at=int(row[8]) if row[8] is not None else None,
        consumed_at=int(row[9]) if row[9] is not None else None,
    )


async def _fetch_challenge(
    conn: aiosqlite.Connection,
    challenge_id: str,
) -> PhoneVerificationChallenge | None:
    cursor = await conn.execute(
        f"SELECT {_CHALLENGE_COLUMNS} FROM PHONE_VERIFICATION_CHALLENGES WHERE id = ?",
        (challenge_id,),
    )
    row = await cursor.fetchone()
    return _challenge_from_row(row) if row else None


def _assert_challenge_binding(
    challenge: PhoneVerificationChallenge | None,
    *,
    actor_user_id: int,
    purpose: str | None = None,
    phone_number: str | None = None,
) -> PhoneVerificationChallenge:
    if challenge is None or challenge.actor_user_id != actor_user_id:
        raise PhoneVerificationNotFoundError()
    if purpose is not None and challenge.purpose != purpose:
        raise PhoneVerificationNotFoundError()
    if phone_number is not None and challenge.phone_number != phone_number:
        raise PhoneVerificationNotFoundError()
    return challenge


async def _window_usage(
    conn: aiosqlite.Connection,
    *,
    where_clause: str,
    params: tuple[Any, ...],
    since: int,
) -> tuple[int, int | None]:
    cursor = await conn.execute(
        f"""
        SELECT COUNT(*), MIN(created_at)
        FROM PHONE_VERIFICATION_CHALLENGES
        WHERE created_at >= ? AND {where_clause}
        """,
        (since, *params),
    )
    row = await cursor.fetchone()
    return int(row[0]), int(row[1]) if row[1] is not None else None


def _raise_if_window_limited(
    *,
    count: int,
    oldest: int | None,
    limit: int,
    window_seconds: int,
    now: int,
    scope: str,
) -> None:
    if count < limit:
        return
    retry_after = window_seconds
    if oldest is not None:
        retry_after = max(1, oldest + window_seconds - now)
    raise PhoneVerificationRateLimitError(
        retry_after=retry_after,
        scope=scope,
    )


async def _set_challenge_status(
    challenge_id: str,
    status: str,
    *,
    connection_factory: ConnectionFactory | None,
    provider_sid: str | None = None,
    expected_statuses: tuple[str, ...] = (),
) -> bool:
    async with _get_connection_context(connection_factory) as conn:
        expected_clause = ""
        params: list[Any] = [status, provider_sid, challenge_id]
        if expected_statuses:
            expected_clause = (
                f" AND status IN ({','.join('?' for _ in expected_statuses)})"
            )
            params.extend(expected_statuses)
        cursor = await conn.execute(
            f"""
            UPDATE PHONE_VERIFICATION_CHALLENGES
            SET status = ?, provider_sid = COALESCE(?, provider_sid)
            WHERE id = ?
            {expected_clause}
            RETURNING id
            """,
            tuple(params),
        )
        updated = await cursor.fetchone()
        await conn.commit()
        return updated is not None


async def request_phone_verification(
    *,
    actor_user_id: int,
    phone_number: str,
    purpose: str | PhoneVerificationPurpose,
    request_ip: str,
    twilio_client: TwilioVerifyClient | None,
    service_sid: str | None,
    limits: PhoneVerificationLimits | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> PhoneVerificationChallenge:
    """Reserve quota durably, then ask Twilio to send a verification code."""

    if twilio_client is None or not service_sid:
        raise PhoneVerificationServiceUnavailableError()
    if not isinstance(actor_user_id, int) or actor_user_id <= 0:
        raise InvalidVerificationRequestError()

    phone_number = normalize_phone_number(phone_number)
    purpose = normalize_verification_purpose(purpose)
    request_ip = (request_ip or "unknown").strip()[:128] or "unknown"
    limits = limits or PhoneVerificationLimits.from_env()

    now = int(time.time())
    challenge_id = secrets.token_urlsafe(32)
    expires_at = now + limits.challenge_ttl_seconds

    async with _get_connection_context(connection_factory) as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "DELETE FROM PHONE_VERIFICATION_CHALLENGES WHERE created_at < ?",
                (now - limits.retention_seconds,),
            )

            cursor = await conn.execute(
                """
                SELECT MAX(created_at)
                FROM PHONE_VERIFICATION_CHALLENGES
                WHERE actor_user_id = ? AND phone_number = ?
                """,
                (actor_user_id, phone_number),
            )
            row = await cursor.fetchone()
            last_sent_at = int(row[0]) if row and row[0] is not None else None
            if (
                limits.cooldown_seconds
                and last_sent_at is not None
                and now - last_sent_at < limits.cooldown_seconds
            ):
                raise PhoneVerificationRateLimitError(
                    retry_after=last_sent_at + limits.cooldown_seconds - now,
                    scope="cooldown",
                )

            hourly_since = now - 3600
            actor_count, actor_oldest = await _window_usage(
                conn,
                where_clause="actor_user_id = ?",
                params=(actor_user_id,),
                since=hourly_since,
            )
            _raise_if_window_limited(
                count=actor_count,
                oldest=actor_oldest,
                limit=limits.actor_hourly_limit,
                window_seconds=3600,
                now=now,
                scope="actor",
            )

            phone_count, phone_oldest = await _window_usage(
                conn,
                where_clause="phone_number = ?",
                params=(phone_number,),
                since=hourly_since,
            )
            _raise_if_window_limited(
                count=phone_count,
                oldest=phone_oldest,
                limit=limits.phone_hourly_limit,
                window_seconds=3600,
                now=now,
                scope="phone",
            )

            ip_count, ip_oldest = await _window_usage(
                conn,
                where_clause="request_ip = ?",
                params=(request_ip,),
                since=hourly_since,
            )
            _raise_if_window_limited(
                count=ip_count,
                oldest=ip_oldest,
                limit=limits.ip_hourly_limit,
                window_seconds=3600,
                now=now,
                scope="ip",
            )

            global_count, global_oldest = await _window_usage(
                conn,
                where_clause="1 = 1",
                params=(),
                since=now - 86400,
            )
            _raise_if_window_limited(
                count=global_count,
                oldest=global_oldest,
                limit=limits.global_daily_limit,
                window_seconds=86400,
                now=now,
                scope="global",
            )

            await conn.execute(
                f"""
                UPDATE PHONE_VERIFICATION_CHALLENGES
                SET status = 'superseded'
                WHERE actor_user_id = ?
                  AND phone_number = ?
                  AND status IN ({','.join('?' for _ in _ACTIVE_STATUSES)})
                """,
                (actor_user_id, phone_number, *_ACTIVE_STATUSES),
            )
            await conn.execute(
                """
                INSERT INTO PHONE_VERIFICATION_CHALLENGES (
                    id, actor_user_id, phone_number, purpose, request_ip,
                    status, verification_attempts, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', 0, ?, ?)
                """,
                (
                    challenge_id,
                    actor_user_id,
                    phone_number,
                    purpose,
                    request_ip,
                    now,
                    expires_at,
                ),
            )
            await conn.commit()

            warning_threshold = math.ceil(limits.global_daily_limit * 0.8)
            if global_count + 1 == warning_threshold:
                logger.warning(
                    "SMS verification volume reached 80% of the configured daily limit"
                )
        except Exception:
            await conn.rollback()
            raise

    try:
        provider_result = await twilio_client.send_verification(
            service_sid, phone_number
        )
    except Exception as exc:
        try:
            await _set_challenge_status(
                challenge_id,
                "provider_error",
                connection_factory=connection_factory,
                expected_statuses=("reserved",),
            )
        except Exception:
            logger.error("Could not record SMS verification provider failure")
        logger.warning(
            "SMS verification provider send failed (%s)", type(exc).__name__
        )
        raise PhoneVerificationProviderError() from None

    if not isinstance(provider_result, Mapping) or provider_result.get("status") != "pending":
        await _set_challenge_status(
            challenge_id,
            "provider_error",
            connection_factory=connection_factory,
            expected_statuses=("reserved",),
        )
        raise PhoneVerificationProviderError()

    provider_sid = provider_result.get("sid")
    if provider_sid is not None:
        provider_sid = str(provider_sid)[:128]
    transitioned = await _set_challenge_status(
        challenge_id,
        "pending",
        provider_sid=provider_sid,
        connection_factory=connection_factory,
        expected_statuses=("reserved",),
    )
    if not transitioned:
        raise PhoneVerificationNotApprovedError()

    return PhoneVerificationChallenge(
        challenge_id=challenge_id,
        actor_user_id=actor_user_id,
        phone_number=phone_number,
        purpose=purpose,
        status="pending",
        verification_attempts=0,
        created_at=now,
        expires_at=expires_at,
    )


async def verify_phone_code(
    *,
    actor_user_id: int,
    challenge_id: str,
    code: str,
    twilio_client: TwilioVerifyClient | None,
    service_sid: str | None,
    purpose: str | PhoneVerificationPurpose | None = None,
    phone_number: str | None = None,
    limits: PhoneVerificationLimits | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> PhoneVerificationChallenge:
    """Approve a challenge after checking its code with Twilio.

    ``purpose`` and ``phone_number`` are optional expected values.  Final
    operations must still pass both to :func:`consume_phone_verification`.
    """

    if twilio_client is None or not service_sid:
        raise PhoneVerificationServiceUnavailableError()
    challenge_id = _validate_challenge_id(challenge_id)
    code = _validate_verification_code(code)
    expected_purpose = (
        normalize_verification_purpose(purpose) if purpose is not None else None
    )
    expected_phone = (
        normalize_phone_number(phone_number) if phone_number is not None else None
    )
    limits = limits or PhoneVerificationLimits.from_env()
    now = int(time.time())

    async with _get_connection_context(connection_factory) as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            challenge = _assert_challenge_binding(
                await _fetch_challenge(conn, challenge_id),
                actor_user_id=actor_user_id,
                purpose=expected_purpose,
                phone_number=expected_phone,
            )

            if challenge.expires_at <= now:
                await conn.execute(
                    """
                    UPDATE PHONE_VERIFICATION_CHALLENGES
                    SET status = 'expired'
                    WHERE id = ? AND status IN ('pending', 'approved')
                    """,
                    (challenge_id,),
                )
                await conn.commit()
                raise PhoneVerificationExpiredError()
            if challenge.status == "approved":
                await conn.commit()
                return challenge
            if challenge.status == "consumed":
                raise PhoneVerificationAlreadyConsumedError()
            if (
                challenge.status == "failed"
                or challenge.verification_attempts
                >= limits.max_verification_attempts
            ):
                if challenge.status != "failed":
                    await conn.execute(
                        "UPDATE PHONE_VERIFICATION_CHALLENGES SET status = 'failed' WHERE id = ?",
                        (challenge_id,),
                    )
                    await conn.commit()
                raise VerificationAttemptsExceededError()
            if challenge.status != "pending":
                raise PhoneVerificationNotApprovedError()

            next_attempt = challenge.verification_attempts + 1
            await conn.execute(
                """
                UPDATE PHONE_VERIFICATION_CHALLENGES
                SET verification_attempts = ?, last_attempt_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (next_attempt, now, challenge_id),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    try:
        provider_result = await twilio_client.check_verification(
            service_sid, challenge.phone_number, code
        )
    except Exception as exc:
        logger.warning(
            "SMS verification provider check failed (%s)", type(exc).__name__
        )
        raise PhoneVerificationProviderError() from None

    if not isinstance(provider_result, Mapping):
        raise PhoneVerificationProviderError()

    if provider_result.get("status") != "approved":
        if next_attempt >= limits.max_verification_attempts:
            await _set_challenge_status(
                challenge_id,
                "failed",
                connection_factory=connection_factory,
                expected_statuses=("pending",),
            )
            raise VerificationAttemptsExceededError()
        raise InvalidVerificationCodeError()

    approval_time = int(time.time())
    async with _get_connection_context(connection_factory) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            f"""
            UPDATE PHONE_VERIFICATION_CHALLENGES
            SET status = 'approved', approved_at = ?
            WHERE id = ?
              AND actor_user_id = ?
              AND status = 'pending'
              AND expires_at > ?
            RETURNING {_CHALLENGE_COLUMNS}
            """,
            (approval_time, challenge_id, actor_user_id, approval_time),
        )
        row = await cursor.fetchone()
        if row:
            await conn.commit()
            return _challenge_from_row(row)

        challenge = _assert_challenge_binding(
            await _fetch_challenge(conn, challenge_id),
            actor_user_id=actor_user_id,
            purpose=expected_purpose,
            phone_number=expected_phone,
        )
        await conn.rollback()
        if challenge.expires_at <= approval_time:
            raise PhoneVerificationExpiredError()
        if challenge.status == "approved":
            return challenge
        if challenge.status == "consumed":
            raise PhoneVerificationAlreadyConsumedError()
        raise PhoneVerificationNotApprovedError()


async def consume_phone_verification(
    conn: aiosqlite.Connection,
    *,
    actor_user_id: int,
    challenge_id: str,
    phone_number: str,
    purpose: str | PhoneVerificationPurpose,
) -> PhoneVerificationChallenge:
    """Consume an approved challenge inside the caller's transaction.

    This function never commits or rolls back ``conn``.  The phone update or
    user creation and this one-shot state transition therefore succeed or fail
    together.
    """

    challenge_id = _validate_challenge_id(challenge_id)
    phone_number = normalize_phone_number(phone_number)
    purpose = normalize_verification_purpose(purpose)
    now = int(time.time())

    cursor = await conn.execute(
        f"""
        UPDATE PHONE_VERIFICATION_CHALLENGES
        SET status = 'consumed', consumed_at = ?
        WHERE id = ?
          AND actor_user_id = ?
          AND phone_number = ?
          AND purpose = ?
          AND status = 'approved'
          AND expires_at > ?
        RETURNING {_CHALLENGE_COLUMNS}
        """,
        (
            now,
            challenge_id,
            actor_user_id,
            phone_number,
            purpose,
            now,
        ),
    )
    row = await cursor.fetchone()
    if row:
        return _challenge_from_row(row)

    challenge = _assert_challenge_binding(
        await _fetch_challenge(conn, challenge_id),
        actor_user_id=actor_user_id,
        purpose=purpose,
        phone_number=phone_number,
    )
    if challenge.expires_at <= now:
        raise PhoneVerificationExpiredError()
    if challenge.status == "consumed":
        raise PhoneVerificationAlreadyConsumedError()
    raise PhoneVerificationNotApprovedError()


__all__ = [
    "PURPOSE_CREATE_USER",
    "PURPOSE_PROFILE_PHONE_CHANGE",
    "PhoneVerificationPurpose",
    "PhoneVerificationLimits",
    "PhoneVerificationChallenge",
    "PhoneVerificationError",
    "InvalidPhoneNumberError",
    "InvalidVerificationRequestError",
    "PhoneVerificationNotFoundError",
    "PhoneVerificationExpiredError",
    "PhoneVerificationNotApprovedError",
    "PhoneVerificationAlreadyConsumedError",
    "InvalidVerificationCodeError",
    "VerificationAttemptsExceededError",
    "PhoneVerificationRateLimitError",
    "PhoneVerificationServiceUnavailableError",
    "PhoneVerificationProviderError",
    "normalize_phone_number",
    "normalize_verification_purpose",
    "request_phone_verification",
    "verify_phone_code",
    "consume_phone_verification",
]
