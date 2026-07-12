from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def test_wizard_is_disabled_by_default_even_if_claude_is_on_path(monkeypatch):
    from marketplace.landing import sandbox
    from marketplace.landing import wizard

    monkeypatch.delenv("LANDING_WIZARD_ENABLED", raising=False)
    monkeypatch.setenv("PATH", "/tmp/fake-claude-bin")

    status = sandbox.get_wizard_sandbox_status()
    assert status.enabled is False
    assert status.available is False
    assert wizard.is_claude_available()[0] is False


def test_enabling_without_a_pinned_verified_runner_still_fails_closed(monkeypatch):
    from marketplace.landing.sandbox import get_wizard_sandbox_status

    monkeypatch.setenv("LANDING_WIZARD_ENABLED", "true")
    monkeypatch.delenv("LANDING_WIZARD_SANDBOX_RUNNER", raising=False)
    monkeypatch.delenv("LANDING_WIZARD_SANDBOX_RUNNER_SHA256", raising=False)

    status = get_wizard_sandbox_status()
    assert status.enabled is True
    assert status.available is False
    assert status.runner_path is None


def test_wizard_command_has_no_host_bypass_or_shell_tool():
    from marketplace.landing.wizard import _claude_command

    command = _claude_command(15)
    joined = " ".join(command)
    assert "bypassPermissions" not in joined
    assert "Bash" not in joined
    assert "acceptEdits" in command
    assert "Write,Read,Edit" in command


def test_start_job_refuses_to_create_worker_without_sandbox(monkeypatch, tmp_path):
    from marketplace.landing import jobs

    monkeypatch.delenv("LANDING_WIZARD_ENABLED", raising=False)
    before = set(jobs.JOBS_DIR.glob("worker_*.py"))
    result = jobs.start_job(
        prompt_id=1,
        job_type="generate",
        prompt_dir=str(tmp_path),
        params={"description": "test"},
    )
    after = set(jobs.JOBS_DIR.glob("worker_*.py"))

    assert result["success"] is False
    assert result["error_code"] == "WIZARD_SANDBOX_UNAVAILABLE"
    assert after == before


def test_sandbox_runner_gets_minimal_environment_and_workspace_only(
    monkeypatch,
    tmp_path,
):
    from marketplace.landing import sandbox

    captured = {}

    monkeypatch.setenv("APP_SECRET_KEY", "must-not-be-inherited")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-inherited")
    monkeypatch.setattr(
        sandbox,
        "get_wizard_sandbox_status",
        lambda: sandbox.WizardSandboxStatus(
            enabled=True,
            available=True,
            reason="verified",
            runner_path="/opt/aurvek/bin/wizard-sandbox",
        ),
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    result = sandbox.run_claude_in_sandbox(
        ["claude", "--allowedTools", "Write,Read,Edit"],
        prompt="create a page",
        workspace=tmp_path,
        timeout=30,
    )

    assert result.returncode == 0
    assert captured["command"][:5] == [
        "/opt/aurvek/bin/wizard-sandbox",
        "--workspace",
        str(tmp_path.resolve()),
        "--timeout-seconds",
        "30",
    ]
    assert captured["command"][5:] == [
        "--",
        "claude",
        "--allowedTools",
        "Write,Read,Edit",
    ]
    assert captured["cwd"] == str(tmp_path.resolve())
    expected_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
    if os.name == "nt" and os.getenv("SystemRoot"):
        expected_env["SystemRoot"] = os.environ["SystemRoot"]
    assert captured["env"] == expected_env
    assert "APP_SECRET_KEY" not in captured["env"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert captured["close_fds"] is True


def test_sandbox_rejects_symlinks_before_invocation(monkeypatch, tmp_path):
    from marketplace.landing import sandbox

    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "escape").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    monkeypatch.setattr(
        sandbox,
        "get_wizard_sandbox_status",
        lambda: sandbox.WizardSandboxStatus(
            enabled=True,
            available=True,
            reason="verified",
            runner_path="/opt/aurvek/bin/wizard-sandbox",
        ),
    )

    with pytest.raises(sandbox.WizardSandboxViolation):
        sandbox.run_claude_in_sandbox(
            ["claude"],
            prompt="test",
            workspace=workspace,
            timeout=30,
        )


@pytest.mark.asyncio
async def test_prompt_wizard_security_guard_is_fail_closed(monkeypatch):
    from marketplace.routes import prompt_landing_builder

    async def unavailable(text):
        return {
            "checked": False,
            "allowed": True,
            "reason": "not configured",
            "threat_level": "none",
            "threats": [],
        }

    monkeypatch.setattr(prompt_landing_builder, "check_security", unavailable)
    response = await prompt_landing_builder._run_security_check(
        "normal request",
        prompt_id=1,
        label="landing wizard",
    )
    assert response is not None
    assert response.status_code == 503


def test_no_bypass_permissions_remains_in_wizard_sources():
    root = Path(__file__).resolve().parents[1]
    source = (root / "marketplace" / "landing" / "wizard.py").read_text(encoding="utf-8")
    assert "bypassPermissions" not in source
