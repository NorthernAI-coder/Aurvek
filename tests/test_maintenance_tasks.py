"""Unit tests for bounded maintenance subprocess execution."""

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import maintenance_tasks


class FakeProcess:
    def __init__(self, *, returncode: int | None = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.communicate = AsyncMock(return_value=(b"", stderr))
        self.kill = Mock()
        self.wait = AsyncMock()


@pytest.mark.parametrize("value", ["0h", "24h", "7d", "2w", "30m"])
def test_validate_audio_cache_age_accepts_supported_values(value):
    assert maintenance_tasks.validate_audio_cache_age(value) == value


@pytest.mark.parametrize(
    "value",
    ["-1h", "24", "1hour", " 24h", "24h ", "", None, 24],
)
def test_validate_audio_cache_age_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        maintenance_tasks.validate_audio_cache_age(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "arguments", "script_name", "timeout"),
    [
        (
            maintenance_tasks.disable_cloudflare_cache,
            (),
            "cloudflare-cache-disabler.py",
            60,
        ),
        (
            maintenance_tasks.clear_audio_cache,
            ("24h",),
            "clear-audio-cache.py",
            300,
        ),
    ],
)
async def test_maintenance_command_uses_bounded_absolute_python_invocation(
    monkeypatch, runner, arguments, script_name, timeout
):
    process = FakeProcess()
    create_process = AsyncMock(return_value=process)

    async def _wait_for(coro, *, timeout):
        return await coro

    wait_for = AsyncMock(side_effect=_wait_for)
    monkeypatch.setattr(
        maintenance_tasks.asyncio, "create_subprocess_exec", create_process
    )
    monkeypatch.setattr(maintenance_tasks.asyncio, "wait_for", wait_for)

    await runner(*arguments)

    create_process.assert_awaited_once()
    call = create_process.await_args
    command = list(call.args)
    project_root = Path(maintenance_tasks.__file__).resolve().parent

    assert command[0] == sys.executable
    assert Path(command[1]).is_absolute()
    assert Path(command[1]) == project_root / script_name
    assert command[2:] == list(arguments)
    assert call.kwargs["cwd"] == str(project_root)
    assert call.kwargs["stdout"] is subprocess.DEVNULL
    assert call.kwargs["stderr"] is subprocess.PIPE
    assert "shell" not in call.kwargs
    wait_for.assert_awaited_once()
    assert wait_for.await_args.kwargs["timeout"] == timeout
    process.communicate.assert_awaited_once()
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_single_lock_rejects_overlapping_cross_task_execution():
    async with maintenance_tasks._maintenance_lock:
        with pytest.raises(maintenance_tasks.MaintenanceTaskBusy):
            await maintenance_tasks.disable_cloudflare_cache()
        with pytest.raises(maintenance_tasks.MaintenanceTaskBusy):
            await maintenance_tasks.clear_audio_cache("24h")


@pytest.mark.asyncio
async def test_nonzero_subprocess_exit_is_reported(monkeypatch):
    process = FakeProcess(returncode=2, stderr=b"simulated failure")
    monkeypatch.setattr(
        maintenance_tasks.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        await maintenance_tasks.disable_cloudflare_cache()

    assert exc_info.value.returncode == 2
    assert exc_info.value.stderr == b"simulated failure"


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_and_reaps_child(monkeypatch):
    process = FakeProcess(returncode=None)
    create_process = AsyncMock(return_value=process)

    async def _time_out(coro, *, timeout):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(
        maintenance_tasks.asyncio, "create_subprocess_exec", create_process
    )
    monkeypatch.setattr(
        maintenance_tasks.asyncio,
        "wait_for",
        AsyncMock(side_effect=_time_out),
    )

    with pytest.raises(maintenance_tasks.MaintenanceTaskTimedOut):
        await maintenance_tasks.disable_cloudflare_cache()

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_task_cancellation_kills_and_reaps_child(monkeypatch):
    process = FakeProcess(returncode=None)

    async def _cancel(coro, *, timeout):
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        maintenance_tasks.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        maintenance_tasks.asyncio,
        "wait_for",
        AsyncMock(side_effect=_cancel),
    )

    with pytest.raises(asyncio.CancelledError):
        await maintenance_tasks.clear_audio_cache("24h")

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()
