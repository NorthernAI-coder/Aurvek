from pathlib import Path

import pytest

from common import resolve_secure_cookie_setting


ROOT = Path(__file__).resolve().parents[1]


def test_cookies_are_secure_by_default_and_production_cannot_opt_out():
    assert resolve_secure_cookie_setting(None, None) is True
    assert resolve_secure_cookie_setting("development", "false") is False
    assert resolve_secure_cookie_setting("test", "false") is False
    assert resolve_secure_cookie_setting("production", "true") is True

    with pytest.raises(RuntimeError, match="only when ENVIRONMENT explicitly"):
        resolve_secure_cookie_setting("production", "false")
    with pytest.raises(RuntimeError, match="only when ENVIRONMENT explicitly"):
        resolve_secure_cookie_setting(None, "false")
    with pytest.raises(RuntimeError, match="either 'true' or 'false'"):
        resolve_secure_cookie_setting("development", "sometimes")


def test_oauth_session_cookie_uses_shared_secure_cookie_policy():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    middleware_start = source.index("app.add_middleware(\n    SessionMiddleware")
    middleware_end = source.index("\n)", middleware_start)
    middleware_config = source[middleware_start:middleware_end]

    assert "https_only=SECURE_COOKIES" in middleware_config
    assert 'same_site="lax"' in middleware_config


def test_nginx_redirect_accepts_https_only_from_trusted_proxies():
    rate_config = (ROOT / "nginx" / "rate_limiting.conf").read_text(encoding="utf-8")
    assert "geo $realip_remote_addr $aurvek_trusted_https_proxy" in rate_config
    assert '"http:https:1" 0;' in rate_config
    assert "default 1;" in rate_config
    assert "map $aurvek_redirect_to_https $aurvek_external_scheme" in rate_config

    for filename in ("aurvek-main.conf", "aurvek-cdn.conf"):
        config = (ROOT / "nginx" / filename).read_text(encoding="utf-8")
        assert "if ($aurvek_redirect_to_https)" in config
        assert "return 308 https://$host$request_uri;" in config
        assert 'Strict-Transport-Security "max-age=31536000"' in config
        assert "includeSubDomains" not in config

    for filename in (
        "aurvek-main.conf",
        "aurvek-custom-domains.conf",
        "snippets/fastapi-proxy.conf",
    ):
        config = (ROOT / "nginx" / filename).read_text(encoding="utf-8")
        assert "proxy_set_header X-Forwarded-Proto $scheme;" not in config
        assert "proxy_set_header X-Forwarded-Proto $aurvek_external_scheme;" in config

    custom_domains = (
        ROOT / "nginx" / "aurvek-custom-domains.conf"
    ).read_text(encoding="utf-8")
    assert "if ($aurvek_redirect_to_https)" in custom_domains
    assert "return 308 https://$host$request_uri;" in custom_domains
    assert 'add_header Strict-Transport-Security' not in custom_domains
