from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_atagia_config_loads_system_config_over_defaults(mock_db):
    import atagia_config

    atagia_config.invalidate_atagia_config_cache()
    async with mock_db() as conn:
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_enabled", "true"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_transport", "http"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_base_url", "http://127.0.0.1:8100"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_service_api_key", "service-key"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_admin_api_key", "admin-key"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_platform_id", "aurvek"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_mode", "personal_assistant"),
        )
        await conn.commit()

    config = await atagia_config.get_atagia_config()
    bridge_config = await atagia_config.get_atagia_bridge_config()

    assert config["atagia_enabled"] == "true"
    assert config["atagia_transport"] == "http"
    assert config["atagia_base_url"] == "http://127.0.0.1:8100"
    assert bridge_config.enabled is True
    assert bridge_config.transport == "http"
    assert bridge_config.base_url == "http://127.0.0.1:8100"
    assert bridge_config.api_key == "service-key"
    assert bridge_config.admin_api_key == "admin-key"
    assert bridge_config.platform_id == "aurvek"
    assert bridge_config.assistant_mode == "personal_assistant"


@pytest.mark.asyncio
async def test_save_atagia_admin_config_preserves_existing_api_key_when_blank(mock_db):
    import atagia_config

    atagia_config.invalidate_atagia_config_cache()
    async with mock_db() as conn:
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_service_api_key", "existing-secret"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_admin_api_key", "existing-admin-secret"),
        )
        await conn.commit()

    saved = await atagia_config.save_atagia_admin_config(
        {
            "enabled": True,
            "transport": "local",
            "db_path": "db/atagia.db",
            "base_url": "",
            "service_api_key": "",
            "admin_api_key": "",
            "mode": "personal_assistant",
            "platform_id": "aurvek",
            "character_id": "",
            "user_persona_id": "",
            "operational_profile": "",
            "incognito": False,
            "timeout_seconds": "12",
        }
    )
    config = await atagia_config.get_atagia_config()

    assert saved["atagia_enabled"] == "true"
    assert saved["atagia_timeout_seconds"] == "12.0"
    assert saved["atagia_platform_id"] == "aurvek"
    assert config["atagia_service_api_key"] == "existing-secret"
    assert config["atagia_admin_api_key"] == "existing-admin-secret"
    assert config["atagia_mode"] == "personal_assistant"


@pytest.mark.asyncio
async def test_preview_atagia_config_uses_saved_api_key_when_form_key_blank(mock_db):
    import atagia_config

    atagia_config.invalidate_atagia_config_cache()
    async with mock_db() as conn:
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_service_api_key", "existing-secret"),
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_admin_api_key", "existing-admin-secret"),
        )
        await conn.commit()

    preview = await atagia_config.preview_bridge_config_from_admin_payload(
        {
            "transport": "http",
            "base_url": "http://127.0.0.1:8100",
            "service_api_key": "",
            "admin_api_key": "",
        }
    )

    assert preview.enabled is True
    assert preview.transport == "http"
    assert preview.api_key == "existing-secret"
    assert preview.admin_api_key == "existing-admin-secret"
    assert preview.platform_id == "aurvek"


@pytest.mark.asyncio
async def test_atagia_global_incognito_is_normalized_off(mock_db):
    import atagia_config

    atagia_config.invalidate_atagia_config_cache()
    async with mock_db() as conn:
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("atagia_incognito", "true"),
        )
        await conn.commit()

    config = await atagia_config.get_atagia_config()
    bridge_config = await atagia_config.get_atagia_bridge_config()
    saved = await atagia_config.save_atagia_admin_config({"incognito": True})

    assert config["atagia_incognito"] == "false"
    assert bridge_config.incognito is False
    assert saved["atagia_incognito"] == "false"
