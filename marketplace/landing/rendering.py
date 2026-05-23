import os
from html import escape

from fastapi.responses import HTMLResponse, FileResponse

from common import slugify
from database import get_db_connection


LANDING_RELATED_LINKS_ENABLED = os.getenv("LANDING_RELATED_LINKS_ENABLED", "1") == "1"
LANDING_RELATED_LINKS_MAX = int(os.getenv("LANDING_RELATED_LINKS_MAX", "4"))

LANDING_MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
}


def landing_404_response() -> HTMLResponse:
    """Return a simple HTML 404 page for landing pages."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>404 - Page Not Found</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               display: flex; align-items: center; justify-content: center;
               min-height: 100vh; margin: 0; background: #f5f5f5; color: #333; }
        .container { text-align: center; padding: 2rem; }
        h1 { font-size: 6rem; margin: 0; color: #ccc; }
        p { font-size: 1.2rem; color: #666; }
    </style>
</head>
<body>
    <div class="container">
        <h1>404</h1>
        <p>Page not found</p>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=404)


def media_type_for_path(path) -> str:
    """Return landing media type for a static file path."""
    return LANDING_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def file_response_for_landing_static(path, *, cache: bool = False) -> FileResponse:
    """Create a FileResponse for a landing static asset."""
    headers = {"Cache-Control": "public, max-age=3600"} if cache else None
    return FileResponse(path, media_type=media_type_for_path(path), headers=headers)


async def get_related_landing_links(prompt_id: int, max_links: int) -> list:
    """Fetch related public prompt landings for internal SEO linking."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT created_by_user_id FROM PROMPTS WHERE id = ?",
            (prompt_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return []
        creator_id = row[0]

        cursor = await conn.execute(
            """
            WITH current_cats AS (
                SELECT category_id FROM PROMPT_CATEGORIES WHERE prompt_id = ?
            )
            SELECT p.name, p.public_id
            FROM PROMPTS p
            LEFT JOIN PROMPT_CATEGORIES pc ON pc.prompt_id = p.id
            WHERE p.public = 1
              AND IFNULL(p.is_unlisted, 0) = 0
              AND p.has_landing_page = 1
              AND p.public_id IS NOT NULL
              AND p.id <> ?
              AND NOT EXISTS (
                  SELECT 1 FROM PROMPT_CUSTOM_DOMAINS d
                  WHERE d.prompt_id = p.id AND d.is_active = 1
                    AND d.verification_status = 1
              )
            GROUP BY p.id
            ORDER BY
              SUM(CASE WHEN pc.category_id IN (SELECT category_id FROM current_cats)
                       THEN 1 ELSE 0 END) DESC,
              CASE WHEN p.created_by_user_id = ? THEN 1 ELSE 0 END DESC,
              IFNULL(p.ranking_score, 0) DESC,
              p.id DESC
            LIMIT ?
            """,
            (prompt_id, prompt_id, creator_id, max_links),
        )

        return [
            {"name": r[0], "url": f"/p/{r[1]}/{slugify(r[0])}/"}
            for r in await cursor.fetchall()
        ]


def build_related_links_html(links: list) -> str:
    """Build a lightweight, self-styled HTML block for related prompt links."""
    items = "".join(
        f'<a href="{lnk["url"]}" style="display:inline-block;padding:0.4rem 0.8rem;'
        f'background:#f5f5f5;border-radius:6px;color:#333;text-decoration:none;'
        f'font-size:0.9rem;">{escape(lnk["name"])}</a>'
        for lnk in links
    )
    return (
        '<section style="max-width:900px;margin:2rem auto;padding:1.5rem 1rem;'
        'border-top:1px solid #e0e0e0;font-family:system-ui,-apple-system,sans-serif;">'
        '<h3 style="font-size:1rem;color:#555;margin:0 0 1rem;font-weight:600;">'
        "Related assistants</h3>"
        f'<nav style="display:flex;flex-wrap:wrap;gap:0.5rem;">{items}</nav>'
        "</section>"
    )


async def inject_related_links(html_content: str, prompt_id: int, *, page: str, is_preview: bool, is_unlisted: bool) -> str:
    if (
        page == "home"
        and not is_preview
        and not is_unlisted
        and LANDING_RELATED_LINKS_ENABLED
        and "</body>" in html_content.lower()
    ):
        related = await get_related_landing_links(prompt_id, LANDING_RELATED_LINKS_MAX)
        if related:
            related_html = build_related_links_html(related)
            html_content = html_content.replace("</body>", related_html + "\n</body>")
            html_content = html_content.replace("</BODY>", related_html + "\n</BODY>")
    return html_content


def inject_prompt_landing_analytics(html_content: str, prompt_id: int, *, is_preview: bool = False) -> str:
    """Inject analytics and purchase helpers into a prompt landing HTML page."""
    if is_preview or "_aurvek_analytics_loaded" in html_content:
        return html_content

    tracking_script = f'''
<!-- Aurvek Analytics Tracking -->
<script>
(function() {{
    if (window._aurvek_analytics_loaded) return;
    window._aurvek_analytics_loaded = true;
    fetch('/api/analytics/track-visit', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
            prompt_id: {prompt_id},
            page_path: window.location.pathname,
            referrer: document.referrer || ''
        }}),
        credentials: 'include'
    }}).catch(function(e) {{ console.log('Analytics:', e); }});
}})();
window.AurvekPurchase = function(promptId) {{
    if (!promptId) promptId = {prompt_id};
    fetch('/api/prompts/' + promptId + '/purchase', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        credentials: 'include'
    }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
        if (data.checkout_url) window.location = data.checkout_url;
        else if (data.free_purchase) window.location = '/chat';
        else if (data.redirect) window.location = data.redirect;
        else if (data.message) alert(data.message);
        else if (data.detail) alert(data.detail);
    }}).catch(function(e) {{ console.error('Purchase error:', e); }});
}};
</script>
'''
    if "</body>" in html_content.lower():
        html_content = html_content.replace("</body>", tracking_script + "</body>")
        html_content = html_content.replace("</BODY>", tracking_script + "</BODY>")
    else:
        html_content += tracking_script
    return html_content


def inject_custom_domain_analytics(html_content: str, prompt_id: int) -> str:
    """Inject analytics into custom-domain prompt landing HTML."""
    if "_aurvek_analytics_loaded" in html_content:
        return html_content

    tracking_script = f'''
<!-- Aurvek Analytics Tracking -->
<script>
(function() {{
    if (window._aurvek_analytics_loaded) return;
    window._aurvek_analytics_loaded = true;
    fetch('/api/analytics/track-visit', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
            prompt_id: {prompt_id},
            page_path: window.location.pathname,
            referrer: document.referrer || ''
        }}),
        credentials: 'include'
    }}).catch(function(e) {{ console.log('Analytics:', e); }});
}})();
</script>
'''
    if "</body>" in html_content.lower():
        html_content = html_content.replace("</body>", tracking_script + "</body>")
        html_content = html_content.replace("</BODY>", tracking_script + "</BODY>")
    else:
        html_content += tracking_script
    return html_content
