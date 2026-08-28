"""Tests for TransportSSH.

asyncssh is reached through a narrow runner seam, so every phase — discovery,
prereq check, push, extract, eval — is driven here without a real host. The
adapter over asyncssh itself is covered by the ssh_localhost integration tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports.ssh import (
    SSHConnectionError,
    TransportSSH,
)


@dataclass
class RunCall:
    command: str
    input: str | None


@dataclass
class FakeSSHRunner:
    """Scripted SSH runner: matches commands by regex, records everything."""

    responses: list[tuple[str, tuple[str, str, int]]] = field(default_factory=list)
    calls: list[RunCall] = field(default_factory=list)
    pushed: list[tuple[Path, str]] = field(default_factory=list)
    closed: bool = False
    default: tuple[str, str, int] = ("", "", 0)

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str, int]:
        self.calls.append(RunCall(command=command, input=input))
        for pattern, response in self.responses:
            if re.search(pattern, command):
                return response
        return self.default

    async def put_file(self, local: Path, remote: str) -> None:
        self.pushed.append((local, remote))

    async def close(self) -> None:
        self.closed = True

    # -- assertions helpers --
    def commands(self) -> list[str]:
        return [c.command for c in self.calls]

    def ran(self, pattern: str) -> bool:
        return any(re.search(pattern, c) for c in self.commands())


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Keep the prereq-check cache out of the developer's real state directory."""
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_state_dir", lambda *a, **k: str(tmp_path / "state"))


def make_transport(runner: FakeSSHRunner, **kwargs) -> TransportSSH:
    async def factory() -> FakeSSHRunner:
        return runner

    kwargs.setdefault("platform", "ubuntu")
    return TransportSSH("test-host", connection_factory=factory, **kwargs)


def qna_ok(text: str = "A: Ubuntu\nI: singular string\nT: 0.1 ms\n"):
    return (text, "", 0)


# A target that reports all deb-family extraction tools present.
PREREQS_OK = (r"prereq-probe", ("dpkg-deb ar tar", "", 0))


# --- evaluation against an already-installed qna ---------------------------


async def test_evaluate_with_installed_qna(qna_output):
    runner = FakeSSHRunner(
        responses=[
            (r"command -v|if \[ -x", ("/opt/BESClient/bin/qna\n", "", 0)),
            (r"-showtypes", (qna_output("single_answer"), "", 0)),
        ]
    )

    result = await make_transport(runner).evaluate_client_relevance("name of operating system")

    assert result.transport == "ssh"
    assert result.host == "test-host"
    assert result.answers == ["Mac OS 15.5"]
    assert result.error_kind is None
    assert result.qna_path == "/opt/BESClient/bin/qna"


async def test_client_relevance_is_piped_on_stdin():
    runner = FakeSSHRunner(
        responses=[(r"-x |command -v", ("/opt/BESClient/bin/qna\n", "", 0)), (r"-showtypes", qna_ok())]
    )

    await make_transport(runner).evaluate_client_relevance("Q: version of client")

    eval_calls = [c for c in runner.calls if "-showtypes" in c.command]
    assert eval_calls, "no eval command issued"
    assert eval_calls[0].input == "version of client\n", "Q: prefix must be stripped for stdin"


async def test_explicit_qna_path_skips_discovery():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    result = await make_transport(runner).evaluate_client_relevance(
        "true", qna_path="/custom/qna"
    )

    assert result.qna_path == "/custom/qna"
    assert "/custom/qna" in " ".join(runner.commands())


async def test_discovery_failure_maps_to_bootstrap():
    runner = FakeSSHRunner(default=("", "", 1))

    result = await make_transport(runner).evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "qna" in (result.error or "").lower()


async def test_relevance_error_maps_to_relevance(qna_output):
    runner = FakeSSHRunner(
        responses=[
            (r"-x |command -v", ("/opt/BESClient/bin/qna\n", "", 0)),
            (r"-showtypes", (qna_output("relevance_error"), "", 0)),
        ]
    )

    result = await make_transport(runner).evaluate_client_relevance("namez of it")

    assert result.error_kind == ERROR_KIND_RELEVANCE


async def test_nonzero_qna_exit_maps_to_qna():
    runner = FakeSSHRunner(
        responses=[
            (r"-x |command -v", ("/opt/BESClient/bin/qna\n", "", 0)),
            (r"-showtypes", ("", "segfault", 139)),
        ]
    )

    result = await make_transport(runner).evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_QNA
    assert result.exit_code == 139


# --- connection failures ---------------------------------------------------


@pytest.mark.parametrize("exc", [OSError("no route to host"), SSHConnectionError("auth failed")])
async def test_connection_failures_map_to_transport(exc):
    async def failing_factory():
        raise exc

    transport = TransportSSH("unreachable", connection_factory=failing_factory, platform="ubuntu")
    result = await transport.evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert result.host == "unreachable"


async def test_timeout_maps_to_transport():
    async def slow_run(command, *, input=None, timeout=None):
        raise TimeoutError("command timed out")

    runner = FakeSSHRunner()
    runner.run = slow_run  # type: ignore[method-assign]

    result = await make_transport(runner).evaluate_client_relevance("true", timeout_s=0.1)

    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_no_exception_escapes_on_unexpected_error():
    async def exploding(command, *, input=None, timeout=None):
        raise RuntimeError("something entirely unexpected")

    runner = FakeSSHRunner()
    runner.run = exploding  # type: ignore[method-assign]

    result = await make_transport(runner).evaluate_client_relevance("true")

    assert result.error_kind is not None


# --- privilege escalation --------------------------------------------------


async def test_become_wraps_the_eval_in_sudo():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    await make_transport(runner, become=True).evaluate_client_relevance(
        "true", qna_path="/opt/qna"
    )

    eval_command = next(c for c in runner.commands() if "-showtypes" in c)
    assert eval_command.startswith("sudo -n ")


async def test_without_become_no_sudo():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    assert not runner.ran(r"^sudo")


# --- provisioning a pinned version ----------------------------------------


@pytest.fixture
def resolved(tmp_path) -> ResolvedQna:
    artifact = tmp_path / "BESAgent-11.0.6.137-ubuntu18.amd64.deb"
    artifact.write_bytes(b"fake deb")
    return ResolvedQna(version="11.0.6.137", artifact_path=artifact)


async def test_present_marker_skips_the_push(resolved):
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("ok", "", 0)),  # marker present
            (r"-showtypes", qna_ok()),
        ]
    )

    result = await make_transport(runner).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert runner.pushed == [], "cached version must not cross the wire again"
    assert result.qna_version == "11.0.6.137"


async def test_absent_marker_pushes_then_extracts(resolved):
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("", "", 1)),  # marker absent
            PREREQS_OK,
            (r"-showtypes", qna_ok()),
        ]
    )

    await make_transport(runner).evaluate_client_relevance("true", qna=resolved)

    assert len(runner.pushed) == 1
    local, remote = runner.pushed[0]
    assert local == resolved.artifact_path
    assert "11.0.6.137" in remote

    # Match the extraction command specifically: the prereq probe also mentions
    # dpkg-deb, so a loose substring match would find the wrong command.
    commands = runner.commands()
    staging_index = next(i for i, c in enumerate(commands) if c.startswith("mkdir -p"))
    extract_index = next(i for i, c in enumerate(commands) if "dpkg-deb -x" in c)
    marker_index = next(i for i, c in enumerate(commands) if "bfrcr-complete" in c and ">" in c)
    assert staging_index < extract_index < marker_index


async def test_extract_uses_temp_dir_then_renames(resolved):
    """A half-extracted tree must never be mistaken for a complete one."""
    runner = FakeSSHRunner(
        responses=[(r"bfrcr-complete", ("", "", 1)), PREREQS_OK, (r"-showtypes", qna_ok())]
    )

    await make_transport(runner).evaluate_client_relevance("true", qna=resolved)

    joined = " ; ".join(runner.commands())
    assert ".partial" in joined or ".tmp" in joined
    assert "mv " in joined


async def test_extraction_failure_maps_to_bootstrap(resolved):
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("", "", 1)),
            (r"dpkg-deb|ar x", ("", "dpkg-deb: not found", 127)),
        ]
    )

    result = await make_transport(runner).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP


async def test_pinned_version_qna_path_is_under_the_version_dir(resolved):
    runner = FakeSSHRunner(
        responses=[(r"bfrcr-complete", ("ok", "", 0)), (r"-showtypes", qna_ok())]
    )

    result = await make_transport(runner).evaluate_client_relevance("true", qna=resolved)

    assert "11.0.6.137" in result.qna_path
    assert result.qna_path.endswith("opt/BESClient/bin/qna")


async def test_missing_prereq_fails_before_transferring(resolved):
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("", "", 1)),
            (r"prereq-probe", ("dpkg-deb ar tar", "", 0)),  # reports what IS present
        ]
    )
    transport = make_transport(runner, target="rhel")

    result = await transport.evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "rpm2cpio" in (result.error or "") or "cpio" in (result.error or "")
    assert runner.pushed == [], "must not transfer an artifact that cannot be extracted"


async def test_prereq_error_names_an_install_command(resolved):
    runner = FakeSSHRunner(
        responses=[(r"bfrcr-complete", ("", "", 1)), (r"prereq-probe", ("", "", 0))]
    )

    result = await make_transport(runner, target="rhel").evaluate_client_relevance(
        "true", qna=resolved
    )

    assert "install" in (result.error or "")


async def test_prereq_check_cached_per_host(resolved, tmp_path):
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("", "", 1)),
            (r"prereq-probe", ("dpkg-deb ar tar", "", 0)),
            (r"-showtypes", qna_ok()),
        ]
    )
    transport = make_transport(runner, state_dir=tmp_path)

    await transport.evaluate_client_relevance("true", qna=resolved)
    probes_after_first = sum(1 for c in runner.commands() if "prereq-probe" in c)
    await transport.evaluate_client_relevance("true", qna=resolved)
    probes_after_second = sum(1 for c in runner.commands() if "prereq-probe" in c)

    assert probes_after_first == 1
    assert probes_after_second == 1, "prereq result should be cached per (host, platform)"


async def test_recheck_prereqs_forces_another_probe(resolved, tmp_path):
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("", "", 1)),
            (r"prereq-probe", ("dpkg-deb ar tar", "", 0)),
            (r"-showtypes", qna_ok()),
        ]
    )
    await make_transport(runner, state_dir=tmp_path).evaluate_client_relevance(
        "true", qna=resolved
    )
    before = sum(1 for c in runner.commands() if "prereq-probe" in c)

    await make_transport(runner, state_dir=tmp_path, recheck_prereqs=True).evaluate_client_relevance(
        "true", qna=resolved
    )

    assert sum(1 for c in runner.commands() if "prereq-probe" in c) > before


# --- old-version flag compatibility ---------------------------------------


async def test_showtypes_unsupported_degrades_to_plain_t(caplog):
    """9.2/9.5-era qna may not know -showtypes; degrade rather than fail."""
    runner = FakeSSHRunner(
        responses=[
            (r"-showtypes", ("", "unknown option -showtypes", 1)),
            (r"-t", ("A: 9.5.13.79\nT: 0.2 ms\n", "", 0)),
        ]
    )

    with caplog.at_level(logging.WARNING):
        result = await make_transport(runner).evaluate_client_relevance(
            "true", qna_path="/opt/qna"
        )

    assert result.error_kind is None
    assert result.answers == ["9.5.13.79"]
    assert result.answer_types == []
    assert any("showtypes" in r.message for r in caplog.records)


# --- connection reuse ------------------------------------------------------


async def test_connection_opened_once_and_reused():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])
    opened = 0

    async def counting_factory():
        nonlocal opened
        opened += 1
        return runner

    transport = TransportSSH(
        "test-host", connection_factory=counting_factory, platform="ubuntu"
    )
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")

    assert opened == 1, "one connection per host, multiplexed across evals"


async def test_aclose_closes_the_connection():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])
    transport = make_transport(runner)
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")

    await transport.aclose()

    assert runner.closed


async def test_writes_nothing_to_stdout(capsys):
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    assert capsys.readouterr().out == ""
