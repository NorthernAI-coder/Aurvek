from fastapi import APIRouter

from billing.routes.creator_payouts import router as creator_payouts_router
from billing.routes.discounts import router as discounts_router
from billing.routes.wallet import router as wallet_router
from billing.stripe_webhooks import router as stripe_webhooks_router


router = APIRouter()
router.include_router(wallet_router)
router.include_router(discounts_router)
router.include_router(creator_payouts_router)
router.include_router(stripe_webhooks_router)
