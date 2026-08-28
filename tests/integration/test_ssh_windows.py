"""Evaluate client relevance on a real Windows endpoint over SSH.

The unit tests drive the PowerShell wrapper through a fake runner, so nothing
else proves the wrapper works against the shell Windows OpenSSH actually gives
you. The whole point of these tests is the *default* shell: on a stock Windows
host that is `cmd.exe`, and every command this package builds for Windows is
PowerShell. Opt in with::

    BFRCR_WINDOWS_SSH_HOST=user@host uv run pytest -m ssh_windows

The host needs key-based SSH auth and a BigFix client installed. Nothing here
modifies the host — in particular the default shell is deliberately left alone.
"""

from __future__ import annotations

import os

import pytest

from bigfix_remote_client_relevance.transports.ssh import TransportSSH

pytestmark = pytest.mark.ssh_windows


def windows_host() -> str:
    host = os.environ.get("BFRCR_WINDOWS_SSH_HOST")
    assert host, "the skip hook should have caught this"
    return host


def split_host(target: str) -> tuple[str, str | None]:
    user, _, name = target.rpartition("@")
    return (name, user or None)


async def test_evaluates_against_a_real_windows_endpoint():
    name, user = split_host(windows_host())
    transport = TransportSSH(name, user=user, target="windows")

    try:
        result = await transport.evaluate_client_relevance(
            "name of operating system", timeout_s=120.0
        )
    finally:
        await transport.aclose()

    assert result.error_kind is None, result.error
    assert result.answers, "a real qna should answer"
    assert result.answer_types == ["string"]
    assert result.qna_path, "discovery should report where qna was found"


async def test_the_default_shell_is_left_alone():
    """The fix wraps commands; it must never reconfigure the endpoint.

    If this host's default shell were PowerShell the wrapper would still work,
    so this is documentation as much as assertion: the tests above pass while
    the login shell stays cmd.exe.
    """
    import asyncio

    completed = await asyncio.to_thread(
        __import__("subprocess").run,
        ["ssh", "-o", "BatchMode=yes", windows_host(), "echo %COMSPEC%"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert "cmd.exe" in completed.stdout.lower(), (
        "this endpoint's login shell is not cmd.exe, so it does not exercise "
        "the case the wrapper exists for"
    )


async def test_stderr_stays_clean():
    """PowerShell emits CLIXML progress records to stderr unless silenced."""
    name, user = split_host(windows_host())
    transport = TransportSSH(name, user=user, target="windows")

    try:
        result = await transport.evaluate_client_relevance("version of client", timeout_s=120.0)
    finally:
        await transport.aclose()

    assert result.error_kind is None, result.error
    assert "CLIXML" not in (result.raw_qna_output or "")
