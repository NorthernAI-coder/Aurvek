from __future__ import annotations

from common import MARKETPLACE_TEMPLATE_FLAGS_DISABLED, templates


def test_global_marketplace_template_default_is_fail_closed():
    assert templates.env.globals["marketplace"] == MARKETPLACE_TEMPLATE_FLAGS_DISABLED
    assert templates.env.globals["marketplace"]["enabled"] is False
    assert templates.env.globals["marketplace"]["discovery_enabled"] is False


def test_navbar_without_marketplace_context_hides_explore_link():
    html = templates.get_template("navbar.html").render(
        branding={"context_type": "platform"},
        is_admin=False,
        is_user=False,
        navbar_avatar_url="",
        navbar_initials="",
        username="",
    )

    assert 'href="/explore"' not in html
