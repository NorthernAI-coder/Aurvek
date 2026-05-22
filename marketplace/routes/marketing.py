"""Static marketplace marketing routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from common import DATA_DIR
from marketplace.config import require_creator_tools_enabled, require_discovery_enabled


router = APIRouter()


def _serve_data_html(filename: str) -> HTMLResponse:
    html_path = DATA_DIR / filename
    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@router.get("/for-creators", response_class=HTMLResponse)
@router.get("/for-creators-landing.html", response_class=HTMLResponse)
async def for_creators_landing():
    require_creator_tools_enabled()
    return _serve_data_html("for-creators-landing.html")


@router.get("/for-agencies", response_class=HTMLResponse)
@router.get("/for-agencies-landing.html", response_class=HTMLResponse)
async def for_agencies_landing():
    require_creator_tools_enabled()
    return _serve_data_html("for-agencies-landing.html")


@router.get("/explore-landing", response_class=HTMLResponse)
@router.get("/explore-landing.html", response_class=HTMLResponse)
async def explore_marketing_landing():
    require_discovery_enabled()
    return _serve_data_html("explore-landing.html")
