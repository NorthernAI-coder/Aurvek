from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from auth import get_current_user
from billing.discounts import DiscountError, validate_discount_code
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, get_template_context, templates
from database import get_db_connection
from models import User


router = APIRouter()


async def _require_admin(current_user: User | None):
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": None,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/admin/create-discount", response_class=HTMLResponse)
async def create_discount(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("discount.html", context)


@router.post("/process-discount")
async def process_discount(
    code: str = Form(...),
    discount: str = Form(...),
    validity_date: str = Form(None),
    usage_limit: str = Form(None),
    unlimited_usage: bool = Form(False),
    unlimited_date: bool = Form(False),
    current_user: User = Depends(get_current_user),
):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    active = True
    unlimited_uses = unlimited_usage
    unlimited_date = unlimited_date

    if unlimited_uses:
        usage_limit = None
    if unlimited_date:
        validity_date = None

    async with get_db_connection() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO discounts
                (code, discount_value, active, validity_date, usage_count, unlimited_usage, unlimited_validity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (code, discount, active, validity_date, usage_limit, unlimited_uses, unlimited_date),
            )
            await conn.commit()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc

    return JSONResponse(content={"message": "Discount code created successfully"})


@router.post("/apply-discount")
async def apply_discount(
    discount_code: str = Form(...),
    amount: float = Form(...),
):
    try:
        discount = await validate_discount_code(discount_code, amount)
    except DiscountError as exc:
        return JSONResponse({"success": False, "message": exc.message}, status_code=400)

    return JSONResponse(
        {
            "success": True,
            "newPrice": discount.final_amount,
            "originalAmount": amount,
        }
    )


@router.get("/admin/discount-list", response_class=HTMLResponse)
async def discount_list(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM discounts")
        discounts = await cursor.fetchall()

    context = await get_template_context(request, current_user)
    context["discounts"] = discounts
    return templates.TemplateResponse("discount_list.html", context)


@router.get("/admin/get-discount/{code}")
async def get_discount(code: str, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT * FROM discounts WHERE code = ?", (code,))
        discount = await cursor.fetchone()

    if not discount:
        raise HTTPException(status_code=404, detail="Discount not found")

    return JSONResponse(content=dict(discount))


@router.post("/admin/update-discount")
async def update_discount(
    code: str = Form(...),
    discount_value: float = Form(...),
    active: bool = Form(...),
    validity_date: Optional[str] = Form(None),
    usage_count: Optional[int] = Form(None),
    unlimited_validity: bool = Form(False),
    unlimited_usage: bool = Form(False),
    current_user: User = Depends(get_current_user),
):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            if unlimited_validity:
                validity_date = None
            if unlimited_usage:
                usage_count = None

            await cursor.execute(
                """
                UPDATE discounts
                SET discount_value = ?, active = ?, validity_date = ?,
                    usage_count = ?, unlimited_validity = ?, unlimited_usage = ?
                WHERE code = ?
                """,
                (
                    discount_value,
                    active,
                    validity_date,
                    usage_count,
                    unlimited_validity,
                    unlimited_usage,
                    code,
                ),
            )
            await conn.commit()
            return JSONResponse(content={"message": "Discount updated successfully"})
        except Exception as exc:
            await conn.rollback()
            raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc


@router.delete("/admin/delete-discount/{code}")
async def delete_discount(code: str, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute("DELETE FROM discounts WHERE code = ?", (code,))
            await conn.commit()
            return JSONResponse(content={"message": "Discount deleted successfully"})
        except Exception as exc:
            await conn.rollback()
            raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc
