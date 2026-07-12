import asyncio
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace

import aiosqlite
import pytest
import pytest_asyncio

from migration_phone_verification_challenges import migrate
from phone_verification import (
    PURPOSE_CREATE_USER,
    PURPOSE_PROFILE_PHONE_CHANGE,
    InvalidPhoneNumberError,
    InvalidVerificationCodeError,
    PhoneVerificationAlreadyConsumedError,
    PhoneVerificationExpiredError,
    PhoneVerificationLimits,
    PhoneVerificationNotApprovedError,
    PhoneVerificationNotFoundError,
    PhoneVerificationProviderError,
    PhoneVerificationRateLimitError,
    VerificationAttemptsExceededError,
    consume_phone_verification,
    normalize_phone_number,
    request_phone_verification,
    verify_phone_code,
)


BASE_LIMITS = PhoneVerificationLimits(
    cooldown_seconds=0,
    actor_hourly_limit=100,
    phone_hourly_limit=100,
    ip_hourly_limit=100,
    global_daily_limit=1000,
    challenge_ttl_seconds=600,
    max_verification_attempts=5,
    retention_seconds=7 * 24 * 60 * 60,
)


class FakeTwilio:
    def __init__(
        self,
        *,
        send_status="pending",
        check_status="approved",
        send_error=None,
        check_error=None,
        reservation_db_path=None,
        send_started=None,
        release_send=None,
    ):
        self.send_status = send_status
        self.check_status = check_status
        self.send_error = send_error
        self.check_error = check_error
        self.reservation_db_path = reservation_db_path
        self.send_started = send_started
        self.release_send = release_send
        self.send_calls = []
        self.check_calls = []

    async def send_verification(self, service_sid, to, channel="sms"):
        if self.reservation_db_path:
            with sqlite3.connect(self.reservation_db_path) as conn:
                row = conn.execute(
                    """
                    SELECT status
                    FROM PHONE_VERIFICATION_CHALLENGES
                    WHERE phone_number = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (to,),
                ).fetchone()
            assert row == ("reserved",)
        self.send_calls.append((service_sid, to, channel))
        if self.send_started is not None:
            self.send_started.set()
        if self.release_send is not None:
            await self.release_send.wait()
        if self.send_error:
            raise self.send_error
        return {"status": self.send_status, "sid": "VE-test"}

    async def check_verification(self, service_sid, to, code):
        self.check_calls.append((service_sid, to, code))
        if self.check_error:
            raise self.check_error
        return {"status": self.check_status}


def _connection_factory(db_path):
    @asynccontextmanager
    async def factory():
        conn = await aiosqlite.connect(db_path, timeout=5)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            await conn.close()

    return factory


@pytest_asyncio.fixture()
async def phone_db(tmp_path):
    db_path = tmp_path / "phone-verification.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE USERS (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL
            );
            """
        )
        conn.executemany(
            "INSERT INTO USERS (id, username) VALUES (?, ?)",
            [(1, "one"), (2, "two"), (3, "three"), (4, "four")],
        )
    migrate(str(db_path))
    return db_path, _connection_factory(str(db_path))


async def _send(
    phone_db,
    client,
    *,
    actor=1,
    phone="+12025550101",
    purpose=PURPOSE_CREATE_USER,
    ip="203.0.113.10",
    limits=BASE_LIMITS,
):
    _, factory = phone_db
    return await request_phone_verification(
        actor_user_id=actor,
        phone_number=phone,
        purpose=purpose,
        request_ip=ip,
        twilio_client=client,
        service_sid="VA-test",
        limits=limits,
        connection_factory=factory,
    )


def test_normalize_phone_number_accepts_e164_and_common_separators():
    assert normalize_phone_number("+34 600-111-222") == "+34600111222"
    assert normalize_phone_number("00 44 (20) 7946 0958") == "+442079460958"


def test_limits_are_configurable_but_keep_safety_bounds(monkeypatch):
    monkeypatch.setenv("SMS_VERIFICATION_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("SMS_VERIFICATION_ACTOR_HOURLY_LIMIT", "7")
    monkeypatch.setenv("SMS_VERIFICATION_PHONE_HOURLY_LIMIT", "2")
    monkeypatch.setenv("SMS_VERIFICATION_IP_HOURLY_LIMIT", "8")
    monkeypatch.setenv("SMS_VERIFICATION_GLOBAL_DAILY_LIMIT", "50")
    monkeypatch.setenv("SMS_VERIFICATION_TTL_SECONDS", "900")
    monkeypatch.setenv("SMS_VERIFICATION_MAX_ATTEMPTS", "10")
    monkeypatch.setenv("SMS_VERIFICATION_RETENTION_SECONDS", "60")

    limits = PhoneVerificationLimits.from_env()

    assert limits.cooldown_seconds == 0
    assert limits.actor_hourly_limit == 7
    assert limits.phone_hourly_limit == 2
    assert limits.ip_hourly_limit == 8
    assert limits.global_daily_limit == 50
    assert limits.challenge_ttl_seconds == 900
    assert limits.max_verification_attempts == 5
    assert limits.retention_seconds == 86400


@pytest.mark.parametrize(
    "phone",
    [
        "600111222",
        "+0123456789",
        "+123",
        "+1234567890123456",
        "+34abc",
        "+١٢٠٢٥٥٥٠١٠١",
    ],
)
def test_normalize_phone_number_rejects_non_e164(phone):
    with pytest.raises(InvalidPhoneNumberError):
        normalize_phone_number(phone)


@pytest.mark.asyncio
async def test_send_commits_reservation_before_calling_provider(phone_db):
    db_path, _ = phone_db
    client = FakeTwilio(reservation_db_path=str(db_path))

    challenge = await _send(phone_db, client)

    assert challenge.status == "pending"
    assert challenge.phone_number == "+12025550101"
    assert client.send_calls == [("VA-test", "+12025550101", "sms")]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT actor_user_id, purpose, request_ip, status, provider_sid
            FROM PHONE_VERIFICATION_CHALLENGES
            WHERE id = ?
            """,
            (challenge.challenge_id,),
        ).fetchone()
    assert row == (1, PURPOSE_CREATE_USER, "203.0.113.10", "pending", "VE-test")


@pytest.mark.asyncio
async def test_send_provider_failure_is_generic_and_attempt_remains_counted(phone_db):
    client = FakeTwilio(
        send_error=RuntimeError("provider-secret +12025550101")
    )

    with pytest.raises(PhoneVerificationProviderError) as exc_info:
        await _send(phone_db, client)

    assert exc_info.value.status_code == 502
    assert "provider-secret" not in str(exc_info.value)
    assert "+12025550101" not in str(exc_info.value)
    db_path, _ = phone_db
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM PHONE_VERIFICATION_CHALLENGES"
        ).fetchone() == ("provider_error",)

    second_client = FakeTwilio()
    with pytest.raises(PhoneVerificationRateLimitError) as rate_exc:
        await _send(
            phone_db,
            second_client,
            phone="+12025550102",
            limits=replace(BASE_LIMITS, actor_hourly_limit=1),
        )
    assert rate_exc.value.scope == "actor"
    assert second_client.send_calls == []


@pytest.mark.asyncio
async def test_cooldown_blocks_before_second_provider_call(phone_db):
    client = FakeTwilio()
    limits = replace(BASE_LIMITS, cooldown_seconds=60)
    await _send(phone_db, client, limits=limits)

    with pytest.raises(PhoneVerificationRateLimitError) as exc_info:
        await _send(phone_db, client, limits=limits)

    assert exc_info.value.scope == "cooldown"
    assert exc_info.value.retry_after > 0
    assert len(client.send_calls) == 1


@pytest.mark.asyncio
async def test_concurrent_resend_sees_committed_reservation(phone_db):
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    client = FakeTwilio(
        send_started=send_started,
        release_send=release_send,
    )
    limits = replace(BASE_LIMITS, cooldown_seconds=60)
    first_send = asyncio.create_task(_send(phone_db, client, limits=limits))
    await asyncio.wait_for(send_started.wait(), timeout=2)

    with pytest.raises(PhoneVerificationRateLimitError) as exc_info:
        await _send(phone_db, client, limits=limits)

    assert exc_info.value.scope == "cooldown"
    assert len(client.send_calls) == 1
    release_send.set()
    await asyncio.wait_for(first_send, timeout=2)


@pytest.mark.asyncio
async def test_superseded_in_flight_send_cannot_resurrect_old_challenge(phone_db):
    class SupersedingTwilio(FakeTwilio):
        def __init__(self):
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def send_verification(self, service_sid, to, channel="sms"):
            self.send_calls.append((service_sid, to, channel))
            if len(self.send_calls) == 1:
                self.first_started.set()
                await self.release_first.wait()
            return {"status": "pending", "sid": f"VE-{len(self.send_calls)}"}

    client = SupersedingTwilio()
    first_send = asyncio.create_task(_send(phone_db, client))
    await asyncio.wait_for(client.first_started.wait(), timeout=2)
    second = await _send(phone_db, client)
    client.release_first.set()

    with pytest.raises(PhoneVerificationNotApprovedError):
        await asyncio.wait_for(first_send, timeout=2)
    db_path, _ = phone_db
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, status
            FROM PHONE_VERIFICATION_CHALLENGES
            ORDER BY rowid
            """
        ).fetchall()
    assert rows[-1] == (second.challenge_id, "pending")
    assert rows[0][1] == "superseded"


@pytest.mark.asyncio
async def test_actor_hourly_limit_is_durable(phone_db):
    client = FakeTwilio()
    limits = replace(BASE_LIMITS, actor_hourly_limit=2)
    await _send(phone_db, client, phone="+12025550101", limits=limits)
    await _send(phone_db, client, phone="+12025550102", limits=limits)

    with pytest.raises(PhoneVerificationRateLimitError) as exc_info:
        await _send(phone_db, client, phone="+12025550103", limits=limits)

    assert exc_info.value.scope == "actor"
    assert len(client.send_calls) == 2


@pytest.mark.asyncio
async def test_phone_hourly_limit_applies_across_actors(phone_db):
    client = FakeTwilio()
    limits = replace(BASE_LIMITS, phone_hourly_limit=2)
    await _send(phone_db, client, actor=1, ip="203.0.113.1", limits=limits)
    await _send(phone_db, client, actor=2, ip="203.0.113.2", limits=limits)

    with pytest.raises(PhoneVerificationRateLimitError) as exc_info:
        await _send(phone_db, client, actor=3, ip="203.0.113.3", limits=limits)

    assert exc_info.value.scope == "phone"
    assert len(client.send_calls) == 2


@pytest.mark.asyncio
async def test_ip_hourly_limit_applies_across_actors(phone_db):
    client = FakeTwilio()
    limits = replace(BASE_LIMITS, ip_hourly_limit=2)
    await _send(phone_db, client, actor=1, phone="+12025550101", limits=limits)
    await _send(phone_db, client, actor=2, phone="+12025550102", limits=limits)

    with pytest.raises(PhoneVerificationRateLimitError) as exc_info:
        await _send(
            phone_db,
            client,
            actor=3,
            phone="+12025550103",
            limits=limits,
        )

    assert exc_info.value.scope == "ip"
    assert len(client.send_calls) == 2


@pytest.mark.asyncio
async def test_global_daily_limit_applies_across_actor_phone_and_ip(phone_db):
    client = FakeTwilio()
    limits = replace(BASE_LIMITS, global_daily_limit=2)
    await _send(
        phone_db,
        client,
        actor=1,
        phone="+12025550101",
        ip="203.0.113.1",
        limits=limits,
    )
    await _send(
        phone_db,
        client,
        actor=2,
        phone="+12025550102",
        ip="203.0.113.2",
        limits=limits,
    )

    with pytest.raises(PhoneVerificationRateLimitError) as exc_info:
        await _send(
            phone_db,
            client,
            actor=3,
            phone="+12025550103",
            ip="203.0.113.3",
            limits=limits,
        )

    assert exc_info.value.scope == "global"
    assert len(client.send_calls) == 2


@pytest.mark.asyncio
async def test_verify_is_bound_to_actor_purpose_and_phone(phone_db):
    client = FakeTwilio()
    challenge = await _send(phone_db, client)
    _, factory = phone_db

    for overrides in (
        {"actor_user_id": 2},
        {"purpose": PURPOSE_PROFILE_PHONE_CHANGE},
        {"phone_number": "+12025550109"},
    ):
        kwargs = {
            "actor_user_id": 1,
            "challenge_id": challenge.challenge_id,
            "code": "123456",
            "twilio_client": client,
            "service_sid": "VA-test",
            "purpose": PURPOSE_CREATE_USER,
            "phone_number": "+12025550101",
            "limits": BASE_LIMITS,
            "connection_factory": factory,
        }
        kwargs.update(overrides)
        with pytest.raises(PhoneVerificationNotFoundError):
            await verify_phone_code(**kwargs)

    assert client.check_calls == []


@pytest.mark.asyncio
async def test_verify_approves_and_is_idempotent(phone_db):
    client = FakeTwilio(check_status="approved")
    challenge = await _send(phone_db, client)
    _, factory = phone_db
    kwargs = {
        "actor_user_id": 1,
        "challenge_id": challenge.challenge_id,
        "code": "123456",
        "twilio_client": client,
        "service_sid": "VA-test",
        "purpose": PURPOSE_CREATE_USER,
        "phone_number": "+12025550101",
        "limits": BASE_LIMITS,
        "connection_factory": factory,
    }

    approved = await verify_phone_code(**kwargs)
    approved_again = await verify_phone_code(**kwargs)

    assert approved.status == "approved"
    assert approved_again.status == "approved"
    assert len(client.check_calls) == 1


@pytest.mark.asyncio
async def test_verify_provider_error_does_not_expose_internal_details(phone_db):
    send_client = FakeTwilio()
    challenge = await _send(phone_db, send_client)
    check_client = FakeTwilio(
        check_error=RuntimeError("provider-secret +12025550101")
    )
    _, factory = phone_db

    with pytest.raises(PhoneVerificationProviderError) as exc_info:
        await verify_phone_code(
            actor_user_id=1,
            challenge_id=challenge.challenge_id,
            code="123456",
            twilio_client=check_client,
            service_sid="VA-test",
            purpose=PURPOSE_CREATE_USER,
            phone_number="+12025550101",
            limits=BASE_LIMITS,
            connection_factory=factory,
        )

    assert "provider-secret" not in str(exc_info.value)
    assert "+12025550101" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_allows_at_most_five_provider_checks(phone_db):
    client = FakeTwilio(check_status="pending")
    challenge = await _send(phone_db, client)
    _, factory = phone_db
    kwargs = {
        "actor_user_id": 1,
        "challenge_id": challenge.challenge_id,
        "code": "123456",
        "twilio_client": client,
        "service_sid": "VA-test",
        "purpose": PURPOSE_CREATE_USER,
        "phone_number": "+12025550101",
        "limits": BASE_LIMITS,
        "connection_factory": factory,
    }

    for _ in range(4):
        with pytest.raises(InvalidVerificationCodeError):
            await verify_phone_code(**kwargs)
    with pytest.raises(VerificationAttemptsExceededError):
        await verify_phone_code(**kwargs)
    with pytest.raises(VerificationAttemptsExceededError):
        await verify_phone_code(**kwargs)

    assert len(client.check_calls) == 5


@pytest.mark.asyncio
async def test_expired_challenge_is_not_checked_with_provider(phone_db):
    client = FakeTwilio()
    challenge = await _send(phone_db, client)
    db_path, factory = phone_db
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE PHONE_VERIFICATION_CHALLENGES SET expires_at = 0 WHERE id = ?",
            (challenge.challenge_id,),
        )

    with pytest.raises(PhoneVerificationExpiredError):
        await verify_phone_code(
            actor_user_id=1,
            challenge_id=challenge.challenge_id,
            code="123456",
            twilio_client=client,
            service_sid="VA-test",
            purpose=PURPOSE_CREATE_USER,
            phone_number="+12025550101",
            limits=BASE_LIMITS,
            connection_factory=factory,
        )

    assert client.check_calls == []


@pytest.mark.asyncio
async def test_consume_is_one_shot_and_does_not_commit_external_transaction(phone_db):
    client = FakeTwilio()
    challenge = await _send(phone_db, client)
    db_path, factory = phone_db
    await verify_phone_code(
        actor_user_id=1,
        challenge_id=challenge.challenge_id,
        code="123456",
        twilio_client=client,
        service_sid="VA-test",
        purpose=PURPOSE_CREATE_USER,
        phone_number="+12025550101",
        limits=BASE_LIMITS,
        connection_factory=factory,
    )

    async with factory() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        consumed = await consume_phone_verification(
            conn,
            actor_user_id=1,
            challenge_id=challenge.challenge_id,
            phone_number="+12025550101",
            purpose=PURPOSE_CREATE_USER,
        )
        assert consumed.status == "consumed"
        await conn.rollback()

    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM PHONE_VERIFICATION_CHALLENGES WHERE id = ?",
            (challenge.challenge_id,),
        ).fetchone()[0]
    assert status == "approved"

    async with factory() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await consume_phone_verification(
            conn,
            actor_user_id=1,
            challenge_id=challenge.challenge_id,
            phone_number="+12025550101",
            purpose=PURPOSE_CREATE_USER,
        )
        await conn.commit()

    async with factory() as conn:
        with pytest.raises(PhoneVerificationAlreadyConsumedError):
            await consume_phone_verification(
                conn,
                actor_user_id=1,
                challenge_id=challenge.challenge_id,
                phone_number="+12025550101",
                purpose=PURPOSE_CREATE_USER,
            )


@pytest.mark.asyncio
async def test_consume_requires_exact_binding_without_changing_state(phone_db):
    client = FakeTwilio()
    challenge = await _send(phone_db, client)
    db_path, factory = phone_db
    await verify_phone_code(
        actor_user_id=1,
        challenge_id=challenge.challenge_id,
        code="123456",
        twilio_client=client,
        service_sid="VA-test",
        limits=BASE_LIMITS,
        connection_factory=factory,
    )

    async with factory() as conn:
        with pytest.raises(PhoneVerificationNotFoundError):
            await consume_phone_verification(
                conn,
                actor_user_id=1,
                challenge_id=challenge.challenge_id,
                phone_number="+12025550101",
                purpose=PURPOSE_PROFILE_PHONE_CHANGE,
            )
        await conn.rollback()

    with sqlite3.connect(db_path) as conn:
        status = conn.execute(
            "SELECT status FROM PHONE_VERIFICATION_CHALLENGES WHERE id = ?",
            (challenge.challenge_id,),
        ).fetchone()[0]
    assert status == "approved"
