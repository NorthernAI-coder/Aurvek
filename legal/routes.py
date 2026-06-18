"""Public legal/support pages used by web, mobile config, and App Store metadata."""

from __future__ import annotations

import html
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse


router = APIRouter()

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _read_data_html(filename: str) -> HTMLResponse:
    path = _DATA_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Page not found")
    return HTMLResponse(
        content=path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/privacy", response_class=HTMLResponse)
@router.get("/privacy.html", response_class=HTMLResponse)
async def privacy_page():
    return _read_data_html("privacy.html")


@router.get("/terms", response_class=HTMLResponse)
@router.get("/terms.html", response_class=HTMLResponse)
async def terms_page():
    return _read_data_html("terms.html")


@router.get("/support", response_class=HTMLResponse)
async def support_page():
    external_url = os.getenv("SUPPORT_URL", "").strip()
    if external_url and not external_url.rstrip("/").endswith("/support"):
        return RedirectResponse(external_url, status_code=302)

    support_email = (
        os.getenv("MOBILE_SUPPORT_EMAIL", "").strip()
        or os.getenv("SUPPORT_EMAIL", "").strip()
        or "support@example.com"
    )
    escaped_email = html.escape(support_email, quote=True)
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aurvek Support</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: #111827; background: #f8fafc; }}
    main {{ max-width: 680px; margin: 0 auto; padding: 56px 24px; }}
    h1 {{ font-size: 2rem; margin: 0 0 16px; }}
    p {{ line-height: 1.6; }}
    a {{ color: #0f766e; }}
  </style>
</head>
<body>
  <main>
    <h1>Aurvek Support</h1>
    <p>For account, safety, billing, or content concerns, contact us at <a href="mailto:{escaped_email}">{escaped_email}</a>.</p>
  </main>
</body>
</html>"""
    return HTMLResponse(body, headers={"Cache-Control": "public, max-age=300"})
