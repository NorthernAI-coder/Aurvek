from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user
from billing.wallet import (
    create_wallet_checkout,
    credit_free_wallet_topup,
    get_payment_success_model,
)
from common import get_template_context, templates
from models import User


router = APIRouter()


@router.get("/payment", response_class=HTMLResponse)
async def get_payment_page(request: Request, current_user: User = Depends(get_current_user)):
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("payment.html", context)


@router.post("/api/stripe/create-checkout-session")
async def create_stripe_checkout_session(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    base_url = str(request.base_url).rstrip("/")
    result = await create_wallet_checkout(data, base_url, current_user.id)
    return JSONResponse(content=result)


@router.get("/payment-success", response_class=HTMLResponse)
async def payment_success_page(
    request: Request,
    session_id: str = None,
    current_user: User = Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    if not session_id:
        return RedirectResponse(url="/payment", status_code=302)

    model = await get_payment_success_model(session_id, current_user.id)
    if not model:
        return RedirectResponse(url="/payment", status_code=302)

    context = await get_template_context(request, current_user)
    context.update(model)
    return templates.TemplateResponse("payment_success.html", context)


@router.post("/api/payment/free-credit")
async def free_credit_payment(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        data = await request.json()
        original_amount = float(data["originalAmount"])
        discount_code = (data.get("discount_code", "") or "").strip()
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Missing field in request data: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request data: {exc}") from exc

    result = await credit_free_wallet_topup(
        user_id=current_user.id,
        original_amount=original_amount,
        discount_code=discount_code,
        description_prefix="Free credit (100% discount)",
        reference_prefix="free_credit",
    )
    return JSONResponse(
        content=jsonable_encoder(
            {
                "message": "Free credit applied successfully",
                "new_balance": result["new_balance"],
                "redirectUrl": "/",
            }
        )
    )
