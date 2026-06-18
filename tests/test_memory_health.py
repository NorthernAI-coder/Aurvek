from __future__ import annotations


def test_disabled_memory_does_not_surface_health_warning():
    from memory.health import (
        get_user_memory_health_snapshot,
        record_memory_failure,
        reset_memory_health_state,
        should_surface_memory_health,
    )

    reset_memory_health_state()
    record_memory_failure("atagia", "context", message="boom")

    snapshot = get_user_memory_health_snapshot("none", enabled=False)

    assert snapshot["status"] == "disabled"
    assert snapshot["should_surface"] is False
    assert should_surface_memory_health(snapshot) is False


def test_user_memory_health_snapshot_is_generic_for_failed_provider():
    from memory.health import (
        get_user_memory_health_snapshot,
        record_memory_failure,
        reset_memory_health_state,
        should_surface_memory_health,
    )

    reset_memory_health_state()
    record_memory_failure(
        "atagia",
        "context",
        message="ModuleNotFoundError: No module named 'atagia'",
    )

    snapshot = get_user_memory_health_snapshot("atagia", enabled=True)

    assert snapshot["status"] == "degraded"
    assert snapshot["should_surface"] is True
    assert "Atagia" not in snapshot["message"]
    assert "atagia" not in snapshot["message"].lower()
    assert should_surface_memory_health(snapshot) is True


def test_memory_health_recovers_after_success_streak():
    from memory.health import (
        get_user_memory_health_snapshot,
        record_memory_failure,
        record_memory_success,
        reset_memory_health_state,
    )

    reset_memory_health_state()
    record_memory_failure("atagia", "test_connection", message="not reachable")

    for _ in range(3):
        record_memory_success("atagia", "context")

    snapshot = get_user_memory_health_snapshot("atagia", enabled=True)

    assert snapshot["status"] == "operational"
    assert snapshot["should_surface"] is False
