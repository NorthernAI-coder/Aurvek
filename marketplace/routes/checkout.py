"""Marketplace checkout routes for prompt and pack purchases."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user
from common import (
    AVATAR_TOKEN_EXPIRE_HOURS,
    CLOUDFLARE_BASE_URL,
    STRIPE_SECRET_KEY,
    get_template_context,
    slugify,
    templates,
    upsert_creator_relationship,
)
from database import get_db_connection
from log_config import logger
from marketplace.config import require_checkout_enabled
from marketplace.services.acquisition import apply_landing_config_to_user
from models import User
from save_images import generate_img_token


router = APIRouter()


@router.get("/pack-purchase-success", response_class=HTMLResponse)
async def pack_purchase_success_page(
    request: Request,
    session_id: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Success page shown after completing a pack purchase via Stripe."""
    require_checkout_enabled()

    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    pack_name = "Pack"
    pack_id = None
    pack_cover_image = None
    pack_creator = None
    pack_landing_url = None
    prompt_count = 0
    payment_amount = None

    if session_id and STRIPE_SECRET_KEY:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            if session and session.metadata.get('user_id', session.metadata.get('buyer_user_id')) == str(current_user.id):
                payment_amount = float(session.metadata.get('final_amount', 0))
                pid = session.metadata.get('pack_id')
                if pid:
                    pack_id = int(pid)
                    async with get_db_connection(readonly=True) as conn:
                        cursor = await conn.cursor()
                        await cursor.execute(
                            """SELECT p.name, p.slug, p.public_id, p.cover_image,
                                      u.username,
                                      (SELECT COUNT(*) FROM PACK_ITEMS pi
                                       WHERE pi.pack_id = p.id AND pi.is_active = 1
                                       AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now')))
                               FROM PACKS p
                               JOIN USERS u ON p.created_by_user_id = u.id
                               WHERE p.id = ?""",
                            (pack_id,)
                        )
                        pack_row = await cursor.fetchone()
                        if pack_row:
                            pack_name = pack_row[0]
                            pack_creator = pack_row[4]
                            prompt_count = pack_row[5]
                            pack_cover_image = pack_row[3]
                            pack_landing_url = f"/pack/{pack_row[2]}/{pack_row[1]}/"
        except Exception as e:
            logger.error(f"Error retrieving pack purchase session: {e}")

    context = await get_template_context(request, current_user)
    context.update({
        "pack_name": pack_name,
        "pack_id": pack_id,
        "pack_cover_image": pack_cover_image,
        "pack_creator": pack_creator,
        "pack_landing_url": pack_landing_url,
        "prompt_count": prompt_count,
        "payment_amount": payment_amount,
    })
    return templates.TemplateResponse("pack_purchase_success.html", context)


# ---- Individual prompt purchase endpoint ----
@router.post("/api/prompts/{prompt_id}/purchase")
async def api_purchase_prompt(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Create a Stripe Checkout Session to purchase an individual prompt."""
    require_checkout_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        body = await request.json()
    except Exception:
        body = {}

    discount_code = str(body.get("discount_code", "")).strip() if body.get("discount_code") else ""

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        # Fetch prompt details
        await cursor.execute(
            "SELECT name, public, purchase_price, created_by_user_id, public_id, description, landing_registration_config FROM PROMPTS WHERE id = ?",
            (prompt_id,)
        )
        prompt_row = await cursor.fetchone()
        if not prompt_row:
            raise HTTPException(status_code=404, detail="Prompt not found")

        prompt_name, is_public, purchase_price, creator_user_id, public_id, prompt_description, landing_reg_config = prompt_row

        if not is_public:
            raise HTTPException(status_code=404, detail="Prompt not found")

        if purchase_price is None or purchase_price == 0:
            raise HTTPException(status_code=400, detail="This prompt is not available for individual purchase")

        # Self-purchase prevention
        if creator_user_id == current_user.id:
            raise HTTPException(status_code=400, detail="You cannot purchase your own prompt")

        # Check existing access via PROMPT_PERMISSIONS
        await cursor.execute(
            "SELECT permission_level FROM PROMPT_PERMISSIONS WHERE prompt_id = ? AND user_id = ?",
            (prompt_id, current_user.id)
        )
        existing_perm = await cursor.fetchone()
        if existing_perm:
            return JSONResponse({"message": "You already have access to this prompt", "redirect": "/chat"})

        # Check existing access via PACK_ACCESS
        await cursor.execute(
            """SELECT 1 FROM PACK_ACCESS pa
               JOIN PACK_ITEMS pi ON pa.pack_id = pi.pack_id
               WHERE pa.user_id = ? AND pi.prompt_id = ?
               AND pi.is_active = 1
               AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
               AND (pa.expires_at IS NULL OR pa.expires_at > datetime('now'))""",
            (current_user.id, prompt_id)
        )
        if await cursor.fetchone():
            return JSONResponse({"message": "You already have access to this prompt", "redirect": "/chat"})

    original_price = float(purchase_price)
    final_amount = original_price
    discount_value = 0

    # Validate and apply discount code
    if discount_code:
        async with get_db_connection(readonly=True) as conn:
            disc_cursor = await conn.execute(
                "SELECT discount_value, active, usage_count, validity_date, unlimited_usage, unlimited_validity FROM DISCOUNTS WHERE code = ?",
                (discount_code,)
            )
            discount = await disc_cursor.fetchone()

            if not discount or not discount[1]:  # not active
                raise HTTPException(status_code=400, detail="Invalid or inactive discount code")

            if not discount[5] and discount[3]:  # not unlimited_validity and has validity_date
                validity = datetime.strptime(discount[3], '%Y-%m-%d').date()
                if date.today() > validity:
                    raise HTTPException(status_code=400, detail="Discount code has expired")

            if not discount[4] and discount[2] is not None:  # not unlimited_usage
                if discount[2] <= 0:
                    raise HTTPException(status_code=400, detail="Discount code usage limit reached")

            discount_value = float(discount[0])
            if discount_value < 0 or discount_value > 100:
                raise HTTPException(status_code=400, detail="Invalid discount value")
            final_amount = max(0, original_price * (1 - discount_value / 100))

    # Reject amounts between $0.01-$0.49 (below Stripe minimum)
    if 0 < final_amount < 0.50:
        raise HTTPException(
            status_code=400,
            detail=f"Final price after discount (${final_amount:.2f}) is below the minimum processing amount ($0.50). The discount must either cover the full price or leave at least $0.50."
        )

    # Generate slug for cancel URL
    prompt_slug = slugify(prompt_name) if prompt_name else ""

    # 100% discount: immediate grant without Stripe
    if final_amount == 0:
        async with get_db_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                # Record purchase
                await conn.execute(
                    """INSERT INTO PROMPT_PURCHASES
                       (buyer_user_id, prompt_id, amount, currency, payment_method, payment_reference, status)
                       VALUES (?, ?, 0.0, 'USD', 'free', ?, 'completed')""",
                    (current_user.id, prompt_id, f"discount_{discount_code}_user_{current_user.id}")
                )

                # Grant access
                await conn.execute(
                    "INSERT OR IGNORE INTO PROMPT_PERMISSIONS (prompt_id, user_id, permission_level) VALUES (?, ?, 'access')",
                    (prompt_id, current_user.id)
                )

                # Set current_prompt_id
                await conn.execute(
                    "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                    (prompt_id, current_user.id)
                )

                # Record transaction
                bal_cur = await conn.execute(
                    "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,)
                )
                bal_row = await bal_cur.fetchone()
                cur_balance = bal_row[0] if bal_row else 0
                await conn.execute('''
                    INSERT INTO TRANSACTIONS
                    (user_id, type, amount, balance_before, balance_after,
                     description, reference_id, discount_code)
                    VALUES (?, 'prompt_purchase', 0, ?, ?, ?, ?, ?)
                ''', (
                    current_user.id,
                    cur_balance,
                    cur_balance,
                    f'Free prompt purchase (100% discount): prompt_id={prompt_id}',
                    f'discount_{discount_code}_user_{current_user.id}',
                    discount_code if discount_code else None
                ))

                # Decrement discount usage
                if discount_code:
                    await conn.execute("""
                        UPDATE DISCOUNTS SET usage_count = CASE
                            WHEN unlimited_usage = 1 THEN usage_count
                            ELSE MAX(0, COALESCE(usage_count, 1) - 1)
                        END WHERE code = ?
                    """, (discount_code,))

                if creator_user_id:
                    try:
                        await upsert_creator_relationship(
                            conn,
                            current_user.id,
                            creator_user_id,
                            "purchased_from",
                            "prompt",
                            prompt_id,
                        )
                    except Exception as ucr_err:
                        logger.warning("Could not record creator relationship for free prompt purchase: %s", ucr_err)

                # Apply landing_registration_config (same as paid purchases)
                if landing_reg_config:
                    try:
                        import json as _json
                        lrc = _json.loads(landing_reg_config) if isinstance(landing_reg_config, str) else landing_reg_config
                        await apply_landing_config_to_user(
                            conn, lrc, current_user.id,
                            creator_user_id=creator_user_id,
                            discount_pct=discount_value)
                    except Exception as lrc_err:
                        logger.warning(f"Failed to apply landing config for free purchase: {lrc_err}")

                await conn.commit()
            except Exception:
                await conn.execute("ROLLBACK")
                raise

        logger.info(f"Free prompt purchase (100% discount): user={current_user.id}, prompt={prompt_id}, code={discount_code}")
        return JSONResponse({
            "message": "Prompt access granted with discount",
            "redirect": "/chat",
            "free_purchase": True
        })

    # Stripe checkout for paid purchases
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment service is not configured")

    base_url = str(request.base_url).rstrip('/')
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': int(final_amount * 100),
                    'product_data': {
                        'name': prompt_name or "AI Prompt",
                        'description': ((prompt_description or "AI prompt")[:500]),
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"{base_url}/prompt-purchase-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/p/{public_id}/{prompt_slug}/?cancelled=true",
            metadata={
                'type': 'prompt_purchase',
                'prompt_id': str(prompt_id),
                'buyer_user_id': str(current_user.id),
                'original_price': str(original_price),
                'final_amount': str(final_amount),
                'discount_code': discount_code,
                'discount_value': str(discount_value),
            }
        )
        return JSONResponse({"checkout_url": session.url})
    except Exception as e:
        logger.error(f"Stripe session creation failed for prompt purchase: {e}")
        raise HTTPException(status_code=500, detail="Payment processing error")


@router.get("/prompt-purchase-success", response_class=HTMLResponse)
async def prompt_purchase_success_page(
    request: Request,
    session_id: str = None,
    current_user: dict = Depends(get_current_user)
):
    """Success page shown after completing a prompt purchase via Stripe."""
    require_checkout_enabled()

    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    prompt_name = "Prompt"
    prompt_id = None
    prompt_image_url = None
    prompt_creator = None
    prompt_landing_url = None
    payment_amount = None

    if session_id and STRIPE_SECRET_KEY:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            if session and session.metadata.get('buyer_user_id') == str(current_user.id):
                payment_amount = float(session.metadata.get('final_amount', 0))
                pid = session.metadata.get('prompt_id')
                if pid:
                    prompt_id = int(pid)
                    async with get_db_connection(readonly=True) as conn:
                        cursor = await conn.cursor()
                        await cursor.execute(
                            """SELECT p.name, p.public_id, p.image,
                                      u.username
                               FROM PROMPTS p
                               JOIN USERS u ON p.created_by_user_id = u.id
                               WHERE p.id = ?""",
                            (prompt_id,)
                        )
                        row = await cursor.fetchone()
                        if row:
                            prompt_name = row[0]
                            prompt_creator = row[3]
                            slug = slugify(row[0]) if row[0] else ""
                            prompt_landing_url = f"/p/{row[1]}/{slug}/" if row[1] else None
                            if row[2]:  # image
                                current_time = datetime.now(timezone.utc)
                                new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
                                img_base = f"{row[2]}_128.webp"
                                token = generate_img_token(img_base, new_expiration, current_user)
                                prompt_image_url = f"{CLOUDFLARE_BASE_URL}{img_base}?token={token}"
        except Exception as e:
            logger.error(f"Error retrieving prompt purchase session: {e}")

    context = await get_template_context(request, current_user)
    context.update({
        "prompt_name": prompt_name,
        "prompt_id": prompt_id,
        "prompt_image_url": prompt_image_url,
        "prompt_creator": prompt_creator,
        "prompt_landing_url": prompt_landing_url,
        "payment_amount": payment_amount,
    })
    return templates.TemplateResponse("prompt_purchase_success.html", context)

