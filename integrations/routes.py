from fastapi import APIRouter

from integrations import platform_routes
from integrations.elevenlabs import admin_routes as elevenlabs_admin_routes
from integrations.elevenlabs import routes as elevenlabs_routes
from integrations.elevenlabs import sdk_routes as elevenlabs_sdk_routes
from integrations.devices import admin_routes as devices_admin_routes
from integrations.devices import routes as devices_routes
from integrations.telegram import admin_routes as telegram_admin_routes
from integrations.telegram import routes as telegram_routes
from integrations.whatsapp import admin_routes as whatsapp_admin_routes
from integrations.whatsapp import routes as whatsapp_routes


router = APIRouter()
router.include_router(platform_routes.router)
router.include_router(elevenlabs_sdk_routes.router)
router.include_router(elevenlabs_admin_routes.router)
router.include_router(elevenlabs_routes.router)
router.include_router(devices_admin_routes.router)
router.include_router(devices_routes.router)
router.include_router(whatsapp_admin_routes.router)
router.include_router(whatsapp_routes.router)
router.include_router(telegram_admin_routes.router)
router.include_router(telegram_routes.router)
