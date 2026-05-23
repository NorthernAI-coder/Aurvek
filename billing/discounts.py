from dataclasses import dataclass
from datetime import date, datetime

from database import get_db_connection
from log_config import logger


class DiscountError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class DiscountResult:
    code: str
    discount_value: float
    original_amount: float
    final_amount: float


async def validate_discount_code(
    code: str,
    amount: float,
    *,
    conn=None,
) -> DiscountResult:
    discount_code = (code or "").strip()
    if not discount_code:
        return DiscountResult("", 0.0, float(amount), float(amount))

    if conn is None:
        async with get_db_connection(readonly=True) as owned_conn:
            return await validate_discount_code(
                discount_code,
                amount,
                conn=owned_conn,
            )

    cursor = await conn.execute(
        """
        SELECT discount_value, active, usage_count, validity_date,
               unlimited_usage, unlimited_validity
        FROM DISCOUNTS
        WHERE code = ?
        """,
        (discount_code,),
    )
    discount = await cursor.fetchone()

    if not discount or not discount["active"]:
        raise DiscountError("Invalid or inactive discount code")

    validity_date = discount["validity_date"]
    if not discount["unlimited_validity"] and validity_date:
        validity = datetime.strptime(validity_date, "%Y-%m-%d").date()
        if date.today() > validity:
            raise DiscountError("Discount code has expired")

    if not discount["unlimited_usage"] and discount["usage_count"] is not None:
        if discount["usage_count"] <= 0:
            raise DiscountError("Discount code usage limit reached")

    discount_value = float(discount["discount_value"])
    if discount_value < 0 or discount_value > 100:
        raise DiscountError("Invalid discount value")

    original_amount = float(amount)
    final_amount = max(0, original_amount * (1 - discount_value / 100))
    return DiscountResult(discount_code, discount_value, original_amount, final_amount)


async def decrement_discount_usage(conn, code: str) -> None:
    discount_code = (code or "").strip()
    if not discount_code:
        return

    await conn.execute(
        """
        UPDATE DISCOUNTS SET usage_count = CASE
            WHEN unlimited_usage = 1 THEN usage_count
            WHEN usage_count IS NULL THEN usage_count
            ELSE MAX(0, COALESCE(usage_count, 1) - 1)
        END
        WHERE code = ?
        """,
        (discount_code,),
    )


async def claim_discount_usage_for_checkout(code: str, amount: float) -> DiscountResult:
    discount_code = (code or "").strip()
    if not discount_code:
        return DiscountResult("", 0.0, float(amount), float(amount))

    async with get_db_connection() as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            discount = await validate_discount_code(discount_code, amount, conn=conn)
            await decrement_discount_usage(conn, discount_code)
            await conn.commit()
            return discount
        except Exception:
            await conn.rollback()
            raise


async def restore_discount_usage_for_checkout(
    code: str,
    *,
    reference_id: str | None = None,
    user_id: int | None = None,
) -> None:
    discount_code = (code or "").strip()
    if not discount_code:
        return

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if reference_id:
            existing = await conn.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ? AND type = 'discount_restored'",
                (reference_id,),
            )
            if await existing.fetchone():
                await conn.rollback()
                return

        await conn.execute(
            """
            UPDATE DISCOUNTS SET usage_count = CASE
                WHEN unlimited_usage = 1 THEN usage_count
                WHEN usage_count IS NULL THEN usage_count
                ELSE usage_count + 1
            END
            WHERE code = ?
            """,
            (discount_code,),
        )
        if reference_id and user_id:
            await conn.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'discount_restored', 0, 0, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    "Discount restored after Stripe Checkout creation did not complete locally",
                    reference_id,
                    discount_code,
                ),
            )
        await conn.commit()


async def restore_discount_usage_for_expired_session(session) -> dict:
    metadata = session.get("metadata", {}) or {}
    if metadata.get("discount_claimed") != "1":
        return {"status": "ignored", "reason": "discount_not_claimed"}

    discount_code = (metadata.get("discount_code") or "").strip()
    if not discount_code:
        return {"status": "ignored", "reason": "missing_discount_code"}

    session_id = getattr(session, "id", None) or session.get("id")
    if not session_id:
        return {"status": "ignored", "reason": "missing_session_id"}
    restore_reference = metadata.get("discount_claim_reference") or session_id

    try:
        user_id = int(metadata.get("user_id") or metadata.get("buyer_user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if user_id <= 0:
        return {"status": "ignored", "reason": "missing_user_id"}

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            existing = await conn.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ? AND type = 'discount_restored'",
                (restore_reference,),
            )
            if await existing.fetchone():
                await conn.rollback()
                return {"status": "already_processed"}

            await conn.execute(
                """
                UPDATE DISCOUNTS SET usage_count = CASE
                    WHEN unlimited_usage = 1 THEN usage_count
                    WHEN usage_count IS NULL THEN usage_count
                    ELSE usage_count + 1
                END
                WHERE code = ?
                """,
                (discount_code,),
            )
            await conn.execute(
                """
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id, discount_code)
                VALUES (?, 'discount_restored', 0, 0, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    "Discount restored after expired Stripe Checkout session",
                    restore_reference,
                    discount_code,
                ),
            )
            await conn.commit()
            logger.info(
                "Restored discount usage after expired Checkout session: session=%s code=%s",
                session_id,
                discount_code,
            )
            return {"status": "success"}
        except Exception:
            await conn.rollback()
            raise
