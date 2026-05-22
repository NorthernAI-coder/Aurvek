from __future__ import annotations

import pytest
from fastapi import HTTPException

import marketplace.config as marketplace


MARKETPLACE_ENV_VARS = (
    "MARKETPLACE_ENABLED",
    "MARKETPLACE_PUBLIC_LANDINGS_ENABLED",
    "MARKETPLACE_CHECKOUT_ENABLED",
    "MARKETPLACE_STOREFRONTS_ENABLED",
    "MARKETPLACE_DISCOVERY_ENABLED",
    "MARKETPLACE_CREATOR_TOOLS_ENABLED",
)


@pytest.fixture(autouse=True)
def clear_marketplace_env(monkeypatch):
    marketplace.reset_marketplace_config_values()
    for name in MARKETPLACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    marketplace.reset_marketplace_config_values()


def test_marketplace_flags_default_to_enabled():
    flags = marketplace.get_marketplace_flags()

    assert flags.enabled is True
    assert flags.public_landings_enabled is True
    assert flags.checkout_enabled is True
    assert flags.storefronts_enabled is True
    assert flags.discovery_enabled is True
    assert flags.creator_tools_enabled is True


def test_master_flag_disables_all_subflags(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    monkeypatch.setenv("MARKETPLACE_PUBLIC_LANDINGS_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_CHECKOUT_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_STOREFRONTS_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_CREATOR_TOOLS_ENABLED", "true")

    flags = marketplace.get_marketplace_flags()

    assert flags.enabled is False
    assert flags.public_landings_enabled is False
    assert flags.checkout_enabled is False
    assert flags.storefronts_enabled is False
    assert flags.discovery_enabled is False
    assert flags.creator_tools_enabled is False

    with pytest.raises(HTTPException) as exc:
        marketplace.require_public_landings_enabled()

    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as checkout_exc:
        marketplace.require_checkout_enabled()

    assert checkout_exc.value.status_code == 404


def test_subflags_can_disable_individual_surfaces(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "true")
    monkeypatch.setenv("MARKETPLACE_DISCOVERY_ENABLED", "0")
    monkeypatch.setenv("MARKETPLACE_CHECKOUT_ENABLED", "off")

    flags = marketplace.get_marketplace_flags()

    assert flags.enabled is True
    assert flags.public_landings_enabled is True
    assert flags.checkout_enabled is False
    assert flags.storefronts_enabled is True
    assert flags.discovery_enabled is False
    assert flags.creator_tools_enabled is True

    with pytest.raises(HTTPException) as exc:
        marketplace.require_checkout_enabled()

    assert exc.value.status_code == 404


def test_system_config_values_drive_flags_when_env_is_absent():
    marketplace.load_marketplace_config_values(
        {
            "marketplace_enabled": "true",
            "marketplace_public_landings_enabled": "false",
            "marketplace_checkout_enabled": "false",
            "marketplace_storefronts_enabled": "true",
            "marketplace_discovery_enabled": "false",
            "marketplace_creator_tools_enabled": "true",
        }
    )

    flags = marketplace.get_marketplace_flags()

    assert flags.enabled is True
    assert flags.public_landings_enabled is False
    assert flags.checkout_enabled is False
    assert flags.storefronts_enabled is True
    assert flags.discovery_enabled is False
    assert flags.creator_tools_enabled is True

    state = marketplace.get_marketplace_config_state()
    assert state["status"] == "partial"
    assert state["runtime_loaded"] is True
    assert state["has_env_overrides"] is False
    assert state["flags"][1]["source"] == "system_config"


def test_environment_overrides_system_config_values(monkeypatch):
    marketplace.load_marketplace_config_values(
        {
            "marketplace_enabled": "true",
            "marketplace_checkout_enabled": "true",
        }
    )
    monkeypatch.setenv("MARKETPLACE_ENABLED", "false")
    monkeypatch.setenv("MARKETPLACE_CHECKOUT_ENABLED", "true")

    flags = marketplace.get_marketplace_flags()
    state = marketplace.get_marketplace_config_state()

    assert flags.enabled is False
    assert flags.checkout_enabled is False
    assert state["status"] == "disabled"
    assert state["has_env_overrides"] is True
    assert state["flags"][0]["source"] == "environment"
    assert state["flags"][0]["env_override"] is True
    assert marketplace.marketplace_config_has_env_override("marketplace_enabled") is True


def test_normalize_marketplace_config_updates_accepts_nested_flags():
    updates = marketplace.normalize_marketplace_config_updates(
        {
            "flags": {
                "marketplace_enabled": False,
                "marketplace_discovery_enabled": "off",
                "marketplace_checkout_enabled": "yes",
            }
        }
    )

    assert updates == {
        "marketplace_enabled": False,
        "marketplace_checkout_enabled": True,
        "marketplace_discovery_enabled": False,
    }


def test_normalize_marketplace_config_updates_rejects_unknown_keys():
    with pytest.raises(ValueError):
        marketplace.normalize_marketplace_config_updates({"flags": {"marketplace_typo": True}})


def test_invalid_flag_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ENABLED", "definitely")
    monkeypatch.setenv("MARKETPLACE_CHECKOUT_ENABLED", "maybe")

    assert marketplace.marketplace_enabled() is True
    assert marketplace.marketplace_checkout_enabled() is True
