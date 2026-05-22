"""Marketplace payment webhook helper functions."""

from __future__ import annotations

from database import get_db_connection
from log_config import logger


async def record_disabled_marketplace_checkout(session, metadata: dict, checkout_type: str) -> str:
    """
    Record a completed marketplace Checkout Session that cannot be fulfilled
    because marketplace checkout is disabled.

    The user has already paid by the time Stripe sends checkout.session.completed.
    Do not grant access or creator earnings here, but do leave an idempotent audit
    trail so Ops can refund or manually resolve the purchase.
    """
    session_id = session.get("id")
    if not session_id:
        logger.error("Marketplace checkout disabled: Stripe session missing id")
        return "missing_session_id"

    try:
        buyer_user_id = int(metadata.get("buyer_user_id") or 0)
    except (TypeError, ValueError):
        buyer_user_id = 0

    if buyer_user_id <= 0:
        logger.error(
            "Marketplace checkout disabled: session %s missing buyer_user_id in metadata",
            session_id,
        )
        return "missing_buyer_user_id"

    try:
        amount = float(metadata.get("final_amount", metadata.get("original_price", 0)) or 0)
    except (TypeError, ValueError):
        amount = 0.0

    discount_code = metadata.get("discount_code") or None
    product_ref = ""
    description = "Marketplace purchase completed while checkout was disabled; refund/manual review required"

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if checkout_type == "pack_purchase":
                fulfilled = await cursor.execute(
                    "SELECT id FROM PACK_PURCHASES WHERE payment_reference = ? AND status = 'completed'",
                    (session_id,),
                )
                if await fulfilled.fetchone():
                    await conn.execute("ROLLBACK")
                    logger.info(
                        "Disabled marketplace checkout webhook was already fulfilled before shutdown: session=%s type=%s",
                        session_id,
                        checkout_type,
                    )
                    return "already_fulfilled"

            elif checkout_type == "prompt_purchase":
                fulfilled = await cursor.execute(
                    "SELECT id FROM PROMPT_PURCHASES WHERE payment_reference = ? AND status = 'completed'",
                    (session_id,),
                )
                if await fulfilled.fetchone():
                    await conn.execute("ROLLBACK")
                    logger.info(
                        "Disabled marketplace checkout webhook was already fulfilled before shutdown: session=%s type=%s",
                        session_id,
                        checkout_type,
                    )
                    return "already_fulfilled"

            existing_tx = await cursor.execute(
                "SELECT id FROM TRANSACTIONS WHERE reference_id = ? AND type = 'marketplace_refund_required'",
                (session_id,),
            )
            if await existing_tx.fetchone():
                await conn.execute("ROLLBACK")
                logger.info(
                    "Disabled marketplace checkout already recorded: session=%s type=%s",
                    session_id,
                    checkout_type,
                )
                return "already_recorded"

            bal_cursor = await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (buyer_user_id,),
            )
            bal_row = await bal_cursor.fetchone()
            balance = bal_row[0] if bal_row else 0

            if checkout_type == "pack_purchase":
                try:
                    pack_id = int(metadata.get("pack_id") or 0)
                except (TypeError, ValueError):
                    pack_id = 0
                product_ref = f"pack_id={pack_id}"

                if pack_id > 0:
                    pack_exists_cursor = await cursor.execute(
                        "SELECT id FROM PACKS WHERE id = ?",
                        (pack_id,),
                    )
                    pack_exists = await pack_exists_cursor.fetchone()
                    existing_purchase = await cursor.execute(
                        "SELECT id FROM PACK_PURCHASES WHERE payment_reference = ?",
                        (session_id,),
                    )
                    if pack_exists and not await existing_purchase.fetchone():
                        await cursor.execute(
                            """INSERT INTO PACK_PURCHASES
                               (buyer_user_id, pack_id, amount, currency, payment_method, payment_reference, status)
                               VALUES (?, ?, ?, 'USD', 'stripe', ?, 'refund_required')""",
                            (buyer_user_id, pack_id, amount, session_id),
                        )

            elif checkout_type == "prompt_purchase":
                try:
                    prompt_id = int(metadata.get("prompt_id") or 0)
                except (TypeError, ValueError):
                    prompt_id = 0
                product_ref = f"prompt_id={prompt_id}"

                if prompt_id > 0:
                    prompt_exists_cursor = await cursor.execute(
                        "SELECT id FROM PROMPTS WHERE id = ?",
                        (prompt_id,),
                    )
                    prompt_exists = await prompt_exists_cursor.fetchone()
                    existing_purchase = await cursor.execute(
                        "SELECT id FROM PROMPT_PURCHASES WHERE payment_reference = ?",
                        (session_id,),
                    )
                    if prompt_exists and not await existing_purchase.fetchone():
                        await cursor.execute(
                            """INSERT OR IGNORE INTO PROMPT_PURCHASES
                               (buyer_user_id, prompt_id, amount, currency, payment_method,
                                payment_reference, discount_code, status)
                               VALUES (?, ?, ?, 'USD', 'stripe', ?, ?, 'refund_required')""",
                            (buyer_user_id, prompt_id, amount, session_id, discount_code),
                        )

            if product_ref:
                description = f"{description}: {product_ref}, paid=${amount:.2f}"

            await cursor.execute(
                """INSERT INTO TRANSACTIONS
                   (user_id, type, amount, balance_before, balance_after,
                    description, reference_id, discount_code)
                   VALUES (?, 'marketplace_refund_required', ?, ?, ?, ?, ?, ?)""",
                (
                    buyer_user_id,
                    amount,
                    balance,
                    balance,
                    description,
                    session_id,
                    discount_code,
                ),
            )

            await conn.commit()
            logger.warning(
                "Recorded disabled marketplace checkout for manual refund/review: session=%s type=%s buyer=%s %s amount=%.2f",
                session_id,
                checkout_type,
                buyer_user_id,
                product_ref,
                amount,
            )
            return "recorded"
        except Exception:
            await conn.rollback()
            logger.exception(
                "Failed to record disabled marketplace checkout: session=%s type=%s",
                session_id,
                checkout_type,
            )
            raise

