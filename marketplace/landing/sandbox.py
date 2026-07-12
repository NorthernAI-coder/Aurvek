"""Fail-closed execution boundary for the creator landing wizard.

The application never invokes Claude Code directly.  Enabling the wizard
requires an operator-installed, root-owned runner whose exact SHA-256 digest
is pinned in configuration and whose self-check implements the
``aurvek-wizard-v1`` sandbox contract.  The runner is responsible for OS-level
isolation (workspace-only filesystem, controlled egress, dropped privileges
and resource limits); Aurvek supplies no application secrets or inherited
environment to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence


_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_RUNNER_CONTRACT = {
    "sandbox": "aurvek-wizard-v1",
    "filesystem": "workspace-only",
    "network": "controlled",
    "privileges": "dropped",
    "resources": "enforced",
}


class WizardSandboxUnavailable(RuntimeError):
    """Raised when a wizard invocation has no verified sandbox boundary."""


class WizardSandboxViolation(RuntimeError):
    """Raised when the workspace violates the sandbox filesystem contract."""


@dataclass(frozen=True, slots=True)
class WizardSandboxStatus:
    enabled: bool
    available: bool
    reason: str
    runner_path: str | None = None


def _enabled() -> bool:
    return os.getenv("LANDING_WIZARD_ENABLED", "false").strip().lower() in _TRUE_VALUES


def _minimal_environment() -> dict[str, str]:
    env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
    if os.name == "nt" and os.getenv("SystemRoot"):
        env["SystemRoot"] = os.environ["SystemRoot"]
    return env


def _has_symlink_component(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=8)
def _validate_runner_cached(
    runner_text: str,
    expected_sha256: str,
    mtime_ns: int,
    ctime_ns: int,
    size: int,
    inode: int,
) -> WizardSandboxStatus:
    # Values deliberately participate in the cache key so replacing the
    # executable invalidates a previously successful verification.
    del mtime_ns, ctime_ns, size, inode
    runner = Path(runner_text)
    try:
        if not runner.is_absolute() or _has_symlink_component(runner):
            raise ValueError("runner path must be absolute and contain no symlinks")
        resolved = runner.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("runner is not a regular file")
        if not os.access(resolved, os.X_OK):
            raise ValueError("runner is not executable")
        if os.name == "posix":
            if metadata.st_uid != 0:
                raise ValueError("runner must be owned by root")
            if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("runner must not be group/world writable")
        if not hmac_compare(_sha256(resolved), expected_sha256):
            raise ValueError("runner digest does not match the pinned SHA-256")

        check = subprocess.run(
            [str(resolved), "--aurvek-sandbox-check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(resolved.parent),
            env=_minimal_environment(),
            close_fds=True,
        )
        if check.returncode != 0:
            raise ValueError("runner self-check failed")
        try:
            contract = json.loads(check.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("runner self-check returned invalid JSON") from exc
        if not isinstance(contract, dict) or any(
            contract.get(key) != value for key, value in _RUNNER_CONTRACT.items()
        ):
            raise ValueError("runner does not satisfy the aurvek-wizard-v1 contract")
        return WizardSandboxStatus(True, True, "verified", str(resolved))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return WizardSandboxStatus(True, False, str(exc), None)


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time digest comparison without importing application secrets."""
    import hmac

    return hmac.compare_digest(left, right)


def get_wizard_sandbox_status() -> WizardSandboxStatus:
    """Return current wizard/sandbox readiness; disabled is the default."""
    if not _enabled():
        return WizardSandboxStatus(False, False, "wizard disabled by configuration")

    runner_text = os.getenv("LANDING_WIZARD_SANDBOX_RUNNER", "").strip()
    expected_sha256 = os.getenv("LANDING_WIZARD_SANDBOX_RUNNER_SHA256", "").strip().lower()
    if not runner_text or not _SHA256_RE.fullmatch(expected_sha256):
        return WizardSandboxStatus(
            True,
            False,
            "sandbox runner and pinned SHA-256 are required",
        )

    runner = Path(runner_text)
    try:
        metadata = runner.lstat()
    except OSError:
        return WizardSandboxStatus(True, False, "sandbox runner is unavailable")
    return _validate_runner_cached(
        runner_text,
        expected_sha256,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_size,
        metadata.st_ino,
    )


def _workspace_symlinks(workspace: Path) -> list[Path]:
    links: list[Path] = []
    for root, directories, filenames in os.walk(workspace, followlinks=False):
        root_path = Path(root)
        for name in list(directories) + filenames:
            candidate = root_path / name
            if candidate.is_symlink():
                links.append(candidate)
    return links


def _remove_symlinks(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def run_claude_in_sandbox(
    claude_args: Sequence[str],
    *,
    prompt: str,
    workspace: str | Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run Claude through the verified external OS sandbox runner."""
    status = get_wizard_sandbox_status()
    if not status.available or not status.runner_path:
        raise WizardSandboxUnavailable(status.reason)

    raw_workspace = Path(workspace)
    if not raw_workspace.is_absolute():
        raw_workspace = raw_workspace.absolute()
    if _has_symlink_component(raw_workspace):
        raise WizardSandboxViolation("workspace path contains a symlink")
    try:
        resolved_workspace = raw_workspace.resolve(strict=True)
    except OSError as exc:
        raise WizardSandboxViolation("workspace does not exist") from exc
    if not resolved_workspace.is_dir():
        raise WizardSandboxViolation("workspace is not a directory")
    if _workspace_symlinks(resolved_workspace):
        raise WizardSandboxViolation("workspace contains symlinks")

    timeout = max(1, min(int(timeout), 60 * 60))
    command = [
        status.runner_path,
        "--workspace",
        str(resolved_workspace),
        "--timeout-seconds",
        str(timeout),
        "--",
        *[str(value) for value in claude_args],
    ]
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 10,
            cwd=str(resolved_workspace),
            env=_minimal_environment(),
            close_fds=True,
        )
    finally:
        generated_links = _workspace_symlinks(resolved_workspace)
        if generated_links:
            _remove_symlinks(generated_links)

    if generated_links:
        raise WizardSandboxViolation("sandbox attempted to create a symlink")
    return result
