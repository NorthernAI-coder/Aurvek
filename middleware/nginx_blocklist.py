"""
NginxBlocklistManager - Dynamic IP blocklist for nginx's geo directive.

Maintains a blocklist file that nginx includes via `geo $blocked_ip`.
When IPs are blocked/unblocked by SecurityTracker, changes are synced
to the file and nginx is reloaded with debounce to avoid excessive reloads.
"""

import asyncio
import ipaddress
import logging
import os
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env vars with sensible defaults)
# ---------------------------------------------------------------------------
_NGINX_BASE = (os.getenv("NGINX_BASE_PATH", "") or "").strip()
NGINX_EXE = (os.getenv("NGINX_BLOCKLIST_EXE", "") or "").strip()
NGINX_CONF = (os.getenv("NGINX_BLOCKLIST_CONF", "") or "").strip()

# Prefix (-p flag) is a Laragon/Windows concept; Linux/macOS nginx
# uses its compiled-in prefix, so only default on Windows.
if os.name == "nt":
    NGINX_PREFIX = (os.getenv("NGINX_BLOCKLIST_PREFIX", _NGINX_BASE) or "").strip()
else:
    NGINX_PREFIX = (os.getenv("NGINX_BLOCKLIST_PREFIX", "") or "").strip()

if not NGINX_EXE:
    if _NGINX_BASE and os.name == "nt":
        NGINX_EXE = os.path.join(_NGINX_BASE, "nginx.exe")
    else:
        # Linux/macOS: nginx is on PATH via package manager
        NGINX_EXE = "nginx"

if not NGINX_CONF and _NGINX_BASE:
    if os.name == "nt":
        # Laragon layout: {base}/conf/nginx.conf
        NGINX_CONF = os.path.join(_NGINX_BASE, "conf", "nginx.conf")
    else:
        # Linux/macOS layout: {base}/nginx.conf
        NGINX_CONF = os.path.join(_NGINX_BASE, "nginx.conf")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKLIST_PATH = os.getenv("NGINX_BLOCKLIST_PATH", os.path.join(_PROJECT_ROOT, "data", "nginx_blocklist.conf"))
DEBOUNCE_SECONDS = int(os.getenv("NGINX_BLOCKLIST_DEBOUNCE", "180"))
RECONCILE_SECONDS = max(10, int(os.getenv("NGINX_BLOCKLIST_RECONCILE_SECONDS", "60")))
MAX_ENTRIES = max(1, int(os.getenv("NGINX_BLOCKLIST_MAX_ENTRIES", "10000")))
ENABLED = os.getenv("NGINX_BLOCKLIST_ENABLED", "true").lower() in ("true", "1", "yes")
RELOAD_MODE = (os.getenv("NGINX_BLOCKLIST_RELOAD_MODE", "direct") or "direct").strip().lower()
if RELOAD_MODE not in {"direct", "external"}:
    logger.warning(
        "NGINX_BLOCKLIST: Unknown reload mode %r; falling back to direct",
        RELOAD_MODE,
    )
    RELOAD_MODE = "direct"


class NginxBlocklistManager:
    """Manages a dynamic nginx IP blocklist with debounced reload."""

    def __init__(self):
        self._blocked_ips: set[str] = set()
        self._dirty: bool = False
        self._last_reload_ts: float = 0.0
        self._generation: int = 0
        self._reload_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_ip(self, ip: str) -> None:
        """Add IP to blocklist. Marks dirty. Skips admin IPs."""
        if not ENABLED:
            return

        normalized_ip = self._normalize_ip(ip)
        if normalized_ip is None:
            logger.warning("NGINX_BLOCKLIST: Ignoring invalid IP: %r", ip)
            return
        if not self._is_edge_eligible(normalized_ip):
            logger.warning("NGINX_BLOCKLIST: Ignoring non-public edge IP: %s", normalized_ip)
            return

        from middleware.security import SecurityConfig
        if normalized_ip in SecurityConfig.get_admin_ips():
            return

        if normalized_ip not in self._blocked_ips:
            if len(self._blocked_ips) >= MAX_ENTRIES:
                logger.error(
                    "NGINX_BLOCKLIST: Entry limit reached (%d); ignoring %s",
                    MAX_ENTRIES,
                    normalized_ip,
                )
                return
            self._blocked_ips.add(normalized_ip)
            self._dirty = True
            self._generation += 1

    def remove_ip(self, ip: str) -> None:
        """Remove IP from blocklist. Marks dirty."""
        if not ENABLED:
            return

        normalized_ip = self._normalize_ip(ip)
        if normalized_ip is None:
            return

        if normalized_ip in self._blocked_ips:
            self._blocked_ips.discard(normalized_ip)
            self._dirty = True
            self._generation += 1

    def reconcile_ips(self, active_ips: Iterable[str], *, remove_missing: bool) -> bool:
        """Reconcile the file state with an active SecurityTracker snapshot."""
        if not ENABLED:
            return False

        normalized: set[str] = set()
        truncated = False
        invalid_snapshot_entry = False
        for ip in active_ips:
            normalized_ip = self._normalize_ip(ip)
            if normalized_ip is None:
                logger.warning("NGINX_BLOCKLIST: Snapshot contains invalid IP: %r", ip)
                invalid_snapshot_entry = True
                continue
            if not self._is_edge_eligible(normalized_ip):
                continue
            if normalized_ip in normalized:
                continue
            if len(normalized) >= MAX_ENTRIES:
                truncated = True
                break
            normalized.add(normalized_ip)

        from middleware.security import SecurityConfig
        normalized.difference_update(SecurityConfig.get_admin_ips())

        if remove_missing and not truncated and not invalid_snapshot_entry:
            desired = normalized
        else:
            desired = set(sorted(self._blocked_ips)[:MAX_ENTRIES])
            additions = sorted(normalized - desired)
            available_slots = max(0, MAX_ENTRIES - len(desired))
            desired.update(additions[:available_slots])
            if len(additions) > available_slots:
                truncated = True

        if desired == self._blocked_ips:
            return False

        self._blocked_ips = desired
        self._dirty = True
        self._generation += 1
        if truncated:
            logger.error(
                "NGINX_BLOCKLIST: Snapshot exceeded %d entries; kept existing entries",
                MAX_ENTRIES,
            )
        return True

    async def maybe_reload(self) -> None:
        """
        If dirty AND debounce period elapsed, write blocklist file and reload nginx.
        Called piggyback on request dispatch (same pattern as reputation flush).
        """
        if not ENABLED or not self._dirty:
            return

        async with self._reload_lock:
            if not self._dirty:
                return

            now = time.time()
            if (now - self._last_reload_ts) < DEBOUNCE_SECONDS:
                return

            await self._flush_pending(now=now, context="update")

    async def initialize(self) -> None:
        """
        Called at app startup (after reputation_manager.initialize()).
        Ensures the blocklist file exists so nginx can start, and loads
        any previously written IPs back into memory.
        """
        if not ENABLED:
            logger.info("NGINX_BLOCKLIST: Disabled via config")
            return

        if not os.path.exists(BLOCKLIST_PATH):
            self._write_blocklist()
            logger.info("NGINX_BLOCKLIST: Created initial empty blocklist")
        else:
            self._load_existing()

        logger.info(
            "NGINX_BLOCKLIST: Initialized with %d IPs (reload_mode=%s)",
            len(self._blocked_ips),
            RELOAD_MODE,
        )

    async def reconcile_once(self) -> None:
        """Reconcile persisted entries with the authoritative security backend."""
        if not ENABLED:
            return

        from middleware.security import get_active_security_block_ips_snapshot_async

        generation_before_snapshot = self._generation
        snapshot = await get_active_security_block_ips_snapshot_async(limit=MAX_ENTRIES)
        self.reconcile_ips(
            snapshot["ips"],
            remove_missing=(
                bool(snapshot["authoritative"])
                and self._generation == generation_before_snapshot
            ),
        )
        if snapshot.get("truncated"):
            logger.error(
                "NGINX_BLOCKLIST: Active backend snapshot reached the %d entry limit; "
                "stale entries will not be removed",
                MAX_ENTRIES,
            )
        await self.maybe_reload()

    async def reconciliation_loop(self) -> None:
        """Periodically remove expired blocks and recover backend changes."""
        while True:
            await asyncio.sleep(RECONCILE_SECONDS)
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("NGINX_BLOCKLIST: Periodic reconciliation failed")

    async def shutdown(self) -> None:
        """Flush final changes if dirty."""
        if not ENABLED or not self._dirty:
            return

        async with self._reload_lock:
            if self._dirty:
                await self._flush_pending(now=time.time(), context="shutdown")

    def _build_nginx_cmd(self, *args: str) -> list[str]:
        """
        Build nginx command safely.
        Only include -p / -c when configured to avoid invalid empty values.
        """
        cmd = [NGINX_EXE, *args]
        if NGINX_PREFIX:
            cmd.extend(["-p", NGINX_PREFIX])
        if NGINX_CONF:
            cmd.extend(["-c", NGINX_CONF])
        return cmd

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_ip(ip: str) -> str | None:
        try:
            address = ipaddress.ip_address((ip or "").strip())
            if getattr(address, "scope_id", None) is not None:
                return None
            return str(address)
        except ValueError:
            return None

    @staticmethod
    def _is_edge_eligible(ip: str) -> bool:
        """External/root promotion accepts only globally routable addresses."""
        return RELOAD_MODE != "external" or ipaddress.ip_address(ip).is_global

    async def _flush_pending(self, *, now: float, context: str) -> None:
        generation = self._generation
        entry_count = len(self._blocked_ips)
        self._write_blocklist()

        if RELOAD_MODE == "external":
            if self._generation == generation:
                self._dirty = False
            self._last_reload_ts = now
            logger.info(
                "NGINX_BLOCKLIST: Wrote %d IPs for external reload (%s)",
                entry_count,
                context,
            )
            return

        ok = await asyncio.to_thread(self._nginx_test)
        if not ok:
            logger.error("NGINX_BLOCKLIST: Config test failed, skipping reload")
            self._last_reload_ts = now
            return

        reloaded = await asyncio.to_thread(self._nginx_reload)
        if reloaded:
            if self._generation == generation:
                self._dirty = False
            logger.info(
                "NGINX_BLOCKLIST: Reloaded with %d blocked IPs (%s)",
                entry_count,
                context,
            )
        else:
            logger.warning("NGINX_BLOCKLIST: Reload failed; keeping pending changes for retry")
        self._last_reload_ts = now

    def _write_blocklist(self) -> None:
        """Atomically write the blocklist staging file in nginx map format."""
        lines = [
            "# Auto-generated by AURVEK NginxBlocklistManager",
            "# DO NOT EDIT MANUALLY - changes will be overwritten",
            f"# Last updated: {datetime.now(timezone.utc).isoformat()}",
        ]
        for ip in sorted(self._blocked_ips):
            lines.append(f"{ip} 1;")

        content = "\n".join(lines) + "\n"

        parent = os.path.dirname(os.path.abspath(BLOCKLIST_PATH))
        os.makedirs(parent, exist_ok=True)

        target_mode = 0o644
        try:
            target_mode = stat.S_IMODE(os.stat(BLOCKLIST_PATH).st_mode)
        except FileNotFoundError:
            pass

        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(prefix=".nginx-blocklist-", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_path, target_mode)
            os.replace(temp_path, BLOCKLIST_PATH)
            temp_path = ""
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _load_existing(self) -> None:
        """Load IPs from existing blocklist file (geo format: 'IP 1;')."""
        try:
            with open(BLOCKLIST_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Expected format: "1.2.3.4 1;"
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == "1;":
                        normalized_ip = self._normalize_ip(parts[0])
                        if normalized_ip is None:
                            logger.warning(
                                "NGINX_BLOCKLIST: Ignoring invalid persisted IP: %r",
                                parts[0],
                            )
                            continue
                        if not self._is_edge_eligible(normalized_ip):
                            logger.warning(
                                "NGINX_BLOCKLIST: Ignoring non-public persisted IP: %s",
                                normalized_ip,
                            )
                            continue
                        if len(self._blocked_ips) >= MAX_ENTRIES:
                            logger.error(
                                "NGINX_BLOCKLIST: Persisted list exceeds %d entries; truncating",
                                MAX_ENTRIES,
                            )
                            break
                        self._blocked_ips.add(normalized_ip)
        except Exception as exc:
            logger.error("NGINX_BLOCKLIST: Failed to load existing file: %s", exc)

    def _nginx_test(self) -> bool:
        """Run nginx -t to validate config. Returns True if valid."""
        try:
            cmd = self._build_nginx_cmd("-t")
            result = subprocess.run(
                cmd,
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                logger.error("NGINX_BLOCKLIST: nginx -t failed: %s", result.stderr.decode(errors="replace"))
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("NGINX_BLOCKLIST: nginx -t timed out")
            return False
        except Exception as exc:
            logger.error("NGINX_BLOCKLIST: nginx -t error: %s", exc)
            return False

    def _nginx_reload(self) -> bool:
        """Send reload signal to nginx. Returns True if reload command succeeds."""
        try:
            cmd = self._build_nginx_cmd("-s", "reload")
            result = subprocess.run(
                cmd,
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                logger.error("NGINX_BLOCKLIST: nginx reload failed: %s", result.stderr.decode(errors="replace"))
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("NGINX_BLOCKLIST: nginx reload timed out")
            return False
        except Exception as exc:
            logger.error("NGINX_BLOCKLIST: nginx reload error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------
nginx_blocklist_manager = NginxBlocklistManager()
