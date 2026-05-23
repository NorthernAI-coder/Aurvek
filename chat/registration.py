from fastapi import APIRouter

from chat.routes import (
    admin,
    attachments,
    bookmarks,
    branching,
    conversations,
    folders,
    media,
    messages,
    pages,
    search,
    voice_io,
    warmup,
)


router = APIRouter()
router.include_router(pages.router)
router.include_router(conversations.router)
router.include_router(messages.router)
router.include_router(warmup.router)
router.include_router(folders.router)
router.include_router(bookmarks.router)
router.include_router(search.router)
router.include_router(branching.router)
router.include_router(attachments.router)
router.include_router(media.router)
router.include_router(voice_io.router)
router.include_router(admin.router)
