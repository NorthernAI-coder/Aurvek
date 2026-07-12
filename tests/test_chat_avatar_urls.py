from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt

from chat.services import avatar_urls
from common import ALGORITHM, AVATAR_TOKEN_EXPIRE_HOURS, SECRET_KEY


def test_get_signed_bot_avatar_urls_binds_each_token_to_its_variant(monkeypatch):
    monkeypatch.setattr(avatar_urls, "CLOUDFLARE_BASE_URL", "https://images.example/")
    image_base_path = "users/aa/bb/user/prompts/001/avatar"
    current_time = datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc)

    urls = avatar_urls.get_signed_bot_avatar_urls(
        image_base_path,
        SimpleNamespace(username="admin"),
        current_time=current_time,
    )

    expected_paths = {
        "bot_profile_picture": f"{image_base_path}_32.webp",
        "bot_profile_picture_128": f"{image_base_path}_128.webp",
        "bot_profile_picture_fullsize": f"{image_base_path}_fullsize.webp",
    }
    assert set(urls) == set(expected_paths)

    for field, expected_path in expected_paths.items():
        parsed_url = urlparse(urls[field])
        token = parse_qs(parsed_url.query)["token"][0]
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )

        assert parsed_url.path == f"/{expected_path}"
        assert payload["media_path"] == expected_path
        assert payload["username"] == "admin"
        assert payload["exp"] == int(current_time.timestamp()) + (
            AVATAR_TOKEN_EXPIRE_HOURS * 60 * 60
        )


def test_get_signed_bot_avatar_urls_returns_stable_empty_payload():
    assert avatar_urls.get_signed_bot_avatar_urls(
        None,
        SimpleNamespace(username="admin"),
    ) == {
        "bot_profile_picture": None,
        "bot_profile_picture_128": None,
        "bot_profile_picture_fullsize": None,
    }
