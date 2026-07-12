"""Bounded execution helpers for administrator maintenance tasks."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent
_AUDIO_CACHE_AGE_PATTERN = re.compile(r"^(?:0h|[1-9]\d{0,5}[hdwm])$")
_maintenance_lock = asyncio.Lock()


class MaintenanceTaskBusy(RuntimeError):
    """Raised when another global maintenance task is already running."""


class MaintenanceTaskTimedOut(RuntimeError):
    """Raised when a maintenance subprocess exceeds its execution limit."""


def validate_audio_cache_age(value: object) -> str:
    """Validate the cache age accepted by ``clear-audio-cache.py``."""
    if not isinstance(value, str) or not _AUDIO_CACHE_AGE_PATTERN.fullmatch(value):
        raise ValueError(
            "time_arg must be 0h or a positive number followed by h, d, w or m."
        )
    return value


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _run_exclusive(
    script_name: str,
    *arguments: str,
    timeout: int,
) -> None:
    if _maintenance_lock.locked():
        raise MaintenanceTaskBusy(f"{script_name} is already running")

    async with _maintenance_lock:
        command = [
            sys.executable,
            str(_PROJECT_ROOT / script_name),
            *arguments,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(_PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError as exc:
            await _kill_process(process)
            raise MaintenanceTaskTimedOut(
                f"{script_name} exceeded its execution limit"
            ) from exc
        except asyncio.CancelledError:
            await _kill_process(process)
            raise

        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                command,
                stderr=stderr,
            )


async def disable_cloudflare_cache() -> None:
    """Activate Cloudflare development mode without blocking the event loop."""
    await _run_exclusive(
        "cloudflare-cache-disabler.py",
        timeout=60,
    )


async def clear_audio_cache(age: object) -> None:
    """Delete audio cache entries older than a validated age."""
    validated_age = validate_audio_cache_age(age)
    await _run_exclusive(
        "clear-audio-cache.py",
        validated_age,
        timeout=300,
    )
