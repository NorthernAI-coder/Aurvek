import asyncio
import threading
import time

import pytest

from middleware import nginx_blocklist as blocklist_module
from middleware.nginx_blocklist import NginxBlocklistManager
from middleware.security import InMemorySecurityBackend, SecurityConfig, SecurityTracker
from nginx.systemd import aurvek_nginx_blocklist_sync as privileged_sync


@pytest.fixture
def no_admin_ips(monkeypatch):
    monkeypatch.setattr(SecurityConfig, "get_admin_ips", classmethod(lambda cls: set()))


@pytest.mark.asyncio
async def test_external_mode_writes_atomically_without_invoking_nginx(
    tmp_path,
    monkeypatch,
    no_admin_ips,
):
    staging = tmp_path / "nginx_blocklist.conf"
    manager = NginxBlocklistManager()

    monkeypatch.setattr(blocklist_module, "BLOCKLIST_PATH", str(staging))
    monkeypatch.setattr(blocklist_module, "RELOAD_MODE", "external")
    monkeypatch.setattr(blocklist_module, "DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(manager, "_nginx_test", lambda: pytest.fail("nginx -t was invoked"))
    monkeypatch.setattr(manager, "_nginx_reload", lambda: pytest.fail("nginx reload was invoked"))

    manager.add_ip("8.8.8.8")
    await manager.maybe_reload()

    assert staging.read_text(encoding="utf-8").endswith("8.8.8.8 1;\n")
    assert manager._dirty is False
    assert not list(tmp_path.glob(".nginx-blocklist-*"))


def test_manager_rejects_injected_entries_and_reconciles_expired_ips(
    no_admin_ips,
):
    manager = NginxBlocklistManager()
    manager.add_ip("8.8.8.8")
    manager.add_ip("1.1.1.1\ninclude /etc/passwd;")

    assert manager._blocked_ips == {"8.8.8.8"}
    assert manager.reconcile_ips(["1.1.1.1"], remove_missing=True) is True
    assert manager._blocked_ips == {"1.1.1.1"}


def test_external_mode_skips_non_public_ips_without_blocking_reconciliation(
    monkeypatch,
    no_admin_ips,
):
    monkeypatch.setattr(blocklist_module, "RELOAD_MODE", "external")
    manager = NginxBlocklistManager()
    manager._blocked_ips = {"8.8.8.8"}

    changed = manager.reconcile_ips(
        ["10.0.0.1", "1.1.1.1"],
        remove_missing=True,
    )

    assert changed is True
    assert manager._blocked_ips == {"1.1.1.1"}


def test_non_authoritative_reconciliation_never_exceeds_entry_limit(
    monkeypatch,
    no_admin_ips,
):
    monkeypatch.setattr(blocklist_module, "MAX_ENTRIES", 2)
    manager = NginxBlocklistManager()
    manager._blocked_ips = {"8.8.8.8", "9.9.9.9"}

    manager.reconcile_ips(["1.1.1.1", "4.4.4.4"], remove_missing=False)

    assert len(manager._blocked_ips) == 2


@pytest.mark.asyncio
async def test_memory_snapshot_prunes_expired_entries():
    backend = InMemorySecurityBackend()
    backend._blocked_ips = {
        "8.8.8.8": time.time() - 1,
        "1.1.1.1": time.time() + 60,
    }
    tracker = SecurityTracker(mode="off", memory_backend=backend)

    snapshot = await tracker.get_active_blocked_ip_snapshot(limit=10)

    assert snapshot == {
        "backend": "memory",
        "ips": ["1.1.1.1"],
        "truncated": False,
        "authoritative": True,
    }


@pytest.mark.asyncio
async def test_direct_reload_does_not_lose_changes_added_during_nginx_test(
    tmp_path,
    monkeypatch,
    no_admin_ips,
):
    manager = NginxBlocklistManager()
    test_started = threading.Event()
    allow_test_to_finish = threading.Event()

    def slow_nginx_test():
        test_started.set()
        assert allow_test_to_finish.wait(timeout=2)
        return True

    monkeypatch.setattr(blocklist_module, "BLOCKLIST_PATH", str(tmp_path / "blocklist.conf"))
    monkeypatch.setattr(blocklist_module, "RELOAD_MODE", "direct")
    monkeypatch.setattr(blocklist_module, "DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(manager, "_nginx_test", slow_nginx_test)
    monkeypatch.setattr(manager, "_nginx_reload", lambda: True)

    manager.add_ip("8.8.8.8")
    reload_task = asyncio.create_task(manager.maybe_reload())
    assert await asyncio.to_thread(test_started.wait, 2)
    manager.add_ip("1.1.1.1")
    allow_test_to_finish.set()
    await reload_task

    assert manager._dirty is True
    await manager.maybe_reload()
    assert manager._dirty is False
    assert "1.1.1.1 1;" in (tmp_path / "blocklist.conf").read_text(encoding="utf-8")


def test_privileged_sync_sanitizes_only_public_ip_entries():
    content, count = privileged_sync.sanitize_candidate(
        "# comment\n8.8.8.8 1;\n2001:4860:4860:0:0:0:0:8888 1;\n8.8.8.8 1;\n"
    )

    assert count == 2
    assert "8.8.8.8 1;" in content
    assert "2001:4860:4860::8888 1;" in content


@pytest.mark.parametrize(
    "line",
    [
        "127.0.0.1 1;",
        "10.0.0.1 1;",
        "8.8.8.0/24 1;",
        "2001:4860:4860::8888%eth0 1;",
        "8.8.8.8 1; include /etc/passwd;",
        "include /etc/nginx/nginx.conf;",
    ],
)
def test_privileged_sync_rejects_unsafe_or_malformed_lines(line):
    with pytest.raises(privileged_sync.SyncError):
        privileged_sync.sanitize_candidate(line + "\n")
