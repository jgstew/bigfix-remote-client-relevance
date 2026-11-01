"""Tests for TransportSSH.

asyncssh is reached through a narrow runner seam, so every phase — discovery,
prereq check, push, extract, eval — is driven here without a real host. The
adapter over asyncssh itself is covered by the ssh_localhost integration tests.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bigfix_remote_client_relevance.bootstrap.targets import spec_for
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
        # Windows commands arrive base64-encoded, so match responses against the
        # PowerShell inside the wrapper — otherwise every pattern would have to
        # be written against opaque base64.
        subject = decode_ps(command) if "-EncodedCommand " in command else command
        for pattern, response in self.responses:
            if re.search(pattern, subject):
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


def make_windows_transport(runner: FakeSSHRunner, **kwargs) -> TransportSSH:
    kwargs["platform"] = "windows"
    return make_transport(runner, **kwargs)


def decode_ps(command: str) -> str:
    """The PowerShell source inside a `-EncodedCommand` wrapper."""
    import base64

    return base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")


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
        responses=[
            (r"-x |command -v", ("/opt/BESClient/bin/qna\n", "", 0)),
            (r"-showtypes", qna_ok()),
        ]
    )

    await make_transport(runner).evaluate_client_relevance("Q: version of client")

    eval_calls = [c for c in runner.calls if "-showtypes" in c.command]
    assert eval_calls, "no eval command issued"
    assert eval_calls[0].input == "version of client\n", "Q: prefix must be stripped for stdin"


async def test_multiline_client_relevance_is_piped_as_one_line():
    runner = FakeSSHRunner(
        responses=[
            (r"-x |command -v", ("/opt/BESClient/bin/qna\n", "", 0)),
            (r"-showtypes", qna_ok()),
        ]
    )

    await make_transport(runner).evaluate_client_relevance("true\nAND false")

    eval_calls = [c for c in runner.calls if "-showtypes" in c.command]
    assert eval_calls, "no eval command issued"
    assert eval_calls[0].input == "true AND false\n"


async def test_explicit_qna_path_skips_discovery():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    result = await make_transport(runner).evaluate_client_relevance("true", qna_path="/custom/qna")

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


# --- platform probing -------------------------------------------------------
#
# The resolver picks the artifact (deb vs rpm vs the Windows zip) from
# target.platform. Left unresolved, orchestrate.py's default_resolver used to
# guess "ubuntu" for any SSH target with no `platform` set -- wrong for every
# non-Linux, non-Debian-family box, and silently so: the wrong artifact just
# got pushed and failed to extract, well after the real (correctly probed)
# platform was already known to _resolve_spec. Exposing that probe as a
# public `resolve_platform()` lets orchestrate.py's existing probe-before-
# resolve step (already used for containers) do the same for SSH.


async def test_probe_output_feeds_resolve_platform():
    runner = FakeSSHRunner(responses=[(r"uname -s", ("Linux\nalmalinux rhel fedora", "", 0))])

    platform = await make_transport(runner, platform=None).resolve_platform()

    assert platform == "rhel"


async def test_empty_probe_output_means_windows():
    """No `uname` at all is exactly what a Windows box's shell reports."""
    runner = FakeSSHRunner(responses=[(r"uname -s", ("", "", 0))])

    platform = await make_transport(runner, platform=None).resolve_platform()

    assert platform == "windows"


async def test_explicit_target_skips_the_probe_for_resolve_platform():
    runner = FakeSSHRunner()

    platform = await make_windows_transport(runner).resolve_platform()

    assert platform == "windows"
    assert not runner.calls, "an explicit target must never probe"


async def test_resolve_platform_probes_once_per_transport():
    runner = FakeSSHRunner(responses=[(r"uname -s", ("Linux\nubuntu debian", "", 0))])
    transport = make_transport(runner, platform=None)

    await transport.resolve_platform()
    await transport.resolve_platform()

    probes = [c for c in runner.commands() if "uname -s" in c]
    assert len(probes) == 1, "probe result must be cached on the transport"


async def test_resolve_platform_shares_the_eval_connection():
    """The probe and the eval must reuse one connection, not open two."""
    runner = FakeSSHRunner(
        responses=[
            (r"uname -s", ("Linux\nubuntu debian", "", 0)),
            (r"-x |command -v", ("/opt/BESClient/bin/qna\n", "", 0)),
            (r"-showtypes", qna_ok()),
        ]
    )
    connects = 0

    async def factory() -> FakeSSHRunner:
        nonlocal connects
        connects += 1
        return runner

    transport = TransportSSH("test-host", connection_factory=factory)
    await transport.resolve_platform()
    await transport.evaluate_client_relevance("true")

    assert connects == 1


# --- arch: a separate uname -m probe, deliberately independent of platform --


async def test_probe_output_feeds_resolve_arch():
    runner = FakeSSHRunner(responses=[(r"uname -m", ("aarch64\n", "", 0))])

    arch = await make_transport(runner, arch=None).resolve_arch()

    assert arch == "arm64", "normalize_arch must fold aarch64 into arm64"


async def test_explicit_arch_skips_the_probe():
    runner = FakeSSHRunner()

    arch = await make_transport(runner, arch="x86_64").resolve_arch()

    assert arch == "x86_64"
    assert not runner.calls, "an explicit arch must never probe"


async def test_resolve_arch_probes_once_per_transport():
    runner = FakeSSHRunner(responses=[(r"uname -m", ("x86_64\n", "", 0))])
    transport = make_transport(runner, arch=None)

    await transport.resolve_arch()
    await transport.resolve_arch()

    probes = [c for c in runner.commands() if "uname -m" in c]
    assert len(probes) == 1, "probe result must be cached on the transport"


async def test_resolve_arch_is_independent_of_the_platform_probe():
    """arch and platform are two separate round trips, not one merged probe."""
    runner = FakeSSHRunner(
        responses=[
            (r"uname -s", ("Linux\nubuntu debian", "", 0)),
            (r"uname -m", ("x86_64\n", "", 0)),
        ]
    )
    transport = make_transport(runner, platform=None, arch=None)

    await transport.resolve_platform()
    await transport.resolve_arch()

    assert runner.ran("uname -s"), "platform probe still ran"
    assert runner.ran("uname -m"), "arch probe still ran, on its own command"


async def test_reprobe_platform_ignores_the_explicit_target():
    """The whole point: ``resolve_platform`` trusts an explicit target, this must not."""
    runner = FakeSSHRunner(responses=[(r"uname -s", ("", "", 0))])  # empty output = windows

    # make_transport defaults platform="ubuntu" -- exactly the wrong,
    # explicit value this method must see past.
    platform = await make_transport(runner).reprobe_platform()

    assert platform == "windows"


async def test_reprobe_platform_reuses_the_connection():
    runner = FakeSSHRunner(responses=[(r"uname -s", ("Linux\nubuntu debian", "", 0))])
    connects = 0

    async def factory() -> FakeSSHRunner:
        nonlocal connects
        connects += 1
        return runner

    transport = TransportSSH("test-host", connection_factory=factory, platform="ubuntu")
    await transport.reprobe_platform()

    assert connects == 1


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

    await make_transport(runner, become=True).evaluate_client_relevance("true", qna_path="/opt/qna")

    eval_command = next(c for c in runner.commands() if "-showtypes" in c)
    assert eval_command.startswith("sudo -n ")


async def test_without_become_no_sudo():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    assert not runner.ran(r"^sudo")


async def test_sudo_password_refusal_is_a_bootstrap_error_not_a_qna_error():
    """A privilege problem is not the relevance engine failing."""
    runner = FakeSSHRunner(responses=[(r"^sudo -n", ("", "sudo: a password is required\n", 1))])

    result = await make_transport(runner, become=True).evaluate_client_relevance(
        "true", qna_path="/opt/qna"
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "sudo" in (result.error or "").lower()
    assert "nopasswd" in (result.error or "").lower()


async def test_sudo_refusal_names_the_remote_qna_path_in_a_sudoers_rule():
    runner = FakeSSHRunner(responses=[(r"^sudo -n", ("", "sudo: a password is required\n", 1))])

    result = await make_transport(runner, become=True, user="admin").evaluate_client_relevance(
        "true", qna_path="/opt/qna"
    )

    assert "admin ALL=(root) NOPASSWD: /opt/qna" in (result.error or "")


async def test_sudo_rule_uses_a_placeholder_when_the_login_is_unknown():
    """With no --user the login comes from ssh config or the local name, which
    this transport never sees -- a guessed name would paste in wrong."""
    runner = FakeSSHRunner(responses=[(r"^sudo -n", ("", "sudo: a password is required\n", 1))])

    result = await make_transport(runner, become=True).evaluate_client_relevance(
        "true", qna_path="/opt/qna"
    )

    assert "<user> ALL=(root) NOPASSWD: /opt/qna" in (result.error or "")


async def test_sudo_refusal_keeps_the_original_sudo_line():
    runner = FakeSSHRunner(
        responses=[(r"^sudo -n", ("", "sudo: user is not in the sudoers file.\n", 1))]
    )

    result = await make_transport(runner, become=True).evaluate_client_relevance(
        "true", qna_path="/opt/qna"
    )

    assert "not in the sudoers file" in (result.error or "")


async def test_qna_failure_under_become_is_still_a_qna_error():
    runner = FakeSSHRunner(responses=[(r"^sudo -n", ("", "qna: bad expression\n", 3))])

    result = await make_transport(runner, become=True).evaluate_client_relevance(
        "true", qna_path="/opt/qna"
    )

    assert result.error_kind == ERROR_KIND_QNA


async def test_relevance_error_under_become_stays_a_relevance_error(qna_output):
    runner = FakeSSHRunner(responses=[(r"^sudo -n", (qna_output("relevance_error"), "", 0))])

    result = await make_transport(runner, become=True).evaluate_client_relevance(
        "namez of it", qna_path="/opt/qna"
    )

    assert result.error_kind == ERROR_KIND_RELEVANCE


async def test_sudo_stderr_is_ignored_without_become():
    """Only a become run may be reclassified; qna stderr must never trigger it."""
    runner = FakeSSHRunner(responses=[(r"-showtypes", ("", "sudo: a password is required\n", 1))])

    result = await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    assert result.error_kind == ERROR_KIND_QNA


# Verbatim shape of a non-root qna on macOS: no A:/E: lines, just libc++'s
# abort. See TransportLocal._macos_root_problem for the local pre-flight.
MACOS_NON_ROOT_CRASH = (
    "",
    "libc++abi: terminating due to uncaught exception of type FileIOError\n",
    134,
)


async def test_macos_qna_crash_without_become_suggests_become():
    """Over SSH the login's euid is unknown without a round trip, so instead of
    a pre-flight refusal the crash itself gets the hint appended."""
    runner = FakeSSHRunner(responses=[(r"-showtypes", MACOS_NON_ROOT_CRASH)])

    result = await make_transport(runner, platform="macos", user="admin").evaluate_client_relevance(
        "true", qna_path="/opt/QnA"
    )

    error = result.error or ""
    assert result.error_kind == ERROR_KIND_QNA
    assert "FileIOError" in error, "the original crash must survive"
    assert "--become" in error
    assert "admin ALL=(root) NOPASSWD: /opt/QnA" in error


async def test_macos_hint_warns_when_the_rule_targets_tmp():
    runner = FakeSSHRunner(responses=[(r"-showtypes", MACOS_NON_ROOT_CRASH)])

    result = await make_transport(runner, platform="macos").evaluate_client_relevance(
        "true", qna_path="/tmp/bigfix_qna/11.0.7.61/BESAgent.app/Contents/MacOS/QnA"
    )

    assert "under /tmp" in (result.error or "")


async def test_macos_hint_is_not_added_under_become():
    runner = FakeSSHRunner(responses=[(r"^sudo -n", MACOS_NON_ROOT_CRASH)])

    result = await make_transport(runner, platform="macos", become=True).evaluate_client_relevance(
        "true", qna_path="/opt/QnA"
    )

    assert "--become" not in (result.error or "")


async def test_macos_hint_is_not_added_off_macos():
    runner = FakeSSHRunner(responses=[(r"-showtypes", MACOS_NON_ROOT_CRASH)])

    result = await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    assert "--become" not in (result.error or "")


async def test_macos_hint_is_not_added_to_a_relevance_error(qna_output):
    """An E: line means qna ran fine, so root is not the problem."""
    runner = FakeSSHRunner(responses=[(r"-showtypes", (qna_output("relevance_error"), "", 0))])

    result = await make_transport(runner, platform="macos").evaluate_client_relevance(
        "namez of it", qna_path="/opt/QnA"
    )

    assert result.error_kind == ERROR_KIND_RELEVANCE
    assert "--become" not in (result.error or "")


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
    await make_transport(runner, state_dir=tmp_path).evaluate_client_relevance("true", qna=resolved)
    before = sum(1 for c in runner.commands() if "prereq-probe" in c)

    await make_transport(
        runner, state_dir=tmp_path, recheck_prereqs=True
    ).evaluate_client_relevance("true", qna=resolved)

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
        result = await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

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

    transport = TransportSSH("test-host", connection_factory=counting_factory, platform="ubuntu")
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")

    assert opened == 1, "one connection per host, multiplexed across evals"


async def test_aclose_closes_the_connection():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])
    transport = make_transport(runner)
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")

    await transport.aclose()

    assert runner.closed


def test_host_keys_are_verified_by_default():
    """Disabling host key checking silently exposes every session to MITM.

    asyncssh treats known_hosts=None as "accept any host key", so it must not
    be passed unless the caller explicitly opts out. Omitting it entirely makes
    asyncssh verify against ~/.ssh/known_hosts, matching the ssh CLI.
    """
    from bigfix_remote_client_relevance.transports.ssh import connect_kwargs

    kwargs = connect_kwargs(user=None, key=None, port=22)

    assert "known_hosts" not in kwargs, "host key verification must stay on by default"


def test_host_key_verification_can_be_disabled_explicitly():
    """Throwaway lab endpoints and CI containers need an opt-out."""
    from bigfix_remote_client_relevance.transports.ssh import connect_kwargs

    kwargs = connect_kwargs(user=None, key=None, port=22, verify_host_key=False)

    assert kwargs["known_hosts"] is None


def test_connect_kwargs_omit_unset_values_rather_than_passing_none():
    """asyncssh rejects an explicit username=None with a bare TypeError.

    Passing None is not the same as omitting the option: option construction
    fails before the connection is even attempted, so a simple typo'd hostname
    surfaces as "'NoneType' object is not iterable" instead of a DNS error.
    """
    from bigfix_remote_client_relevance.transports.ssh import connect_kwargs

    kwargs = connect_kwargs(user=None, key=None, port=22)

    assert None not in kwargs.values() or "known_hosts" in kwargs
    assert "username" not in kwargs
    assert "client_keys" not in kwargs
    assert kwargs["port"] == 22


def test_connect_kwargs_include_values_that_were_given():
    from bigfix_remote_client_relevance.transports.ssh import connect_kwargs

    kwargs = connect_kwargs(user="labadmin", key="/keys/id_ed25519", port=2222)

    assert kwargs["username"] == "labadmin"
    assert kwargs["client_keys"] == ["/keys/id_ed25519"]
    assert kwargs["port"] == 2222


# --- host key lookup is case-insensitive, like the ssh CLI -----------------
#
# OpenSSH lowercases the hostname before matching known_hosts; asyncssh
# matches it verbatim. An inventory host spelled "jgstews-Mac-mini.local"
# was rejected as untrusted even though known_hosts held the right keys under
# "jgstews-mac-mini.local".

MIXED_CASE_HOST = "jgstews-Mac-mini.local"


def test_asyncssh_known_hosts_matching_is_case_sensitive():
    """Root cause: asyncssh does not case-fold the host when matching.

    If this starts failing, asyncssh now case-folds on its own and the
    host_key_alias workaround in connect_kwargs is no longer needed.
    """
    import asyncssh

    public_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key()
    known_hosts = asyncssh.import_known_hosts(f"{MIXED_CASE_HOST.lower()} {public_key.decode()}")

    assert known_hosts.match(MIXED_CASE_HOST.lower(), "", None)[0]
    assert not known_hosts.match(MIXED_CASE_HOST, "", None)[0]


def test_connect_kwargs_look_up_host_key_by_lowercased_host():
    from bigfix_remote_client_relevance.transports.ssh import connect_kwargs

    kwargs = connect_kwargs(user=None, key=None, port=22, host=MIXED_CASE_HOST)

    assert kwargs["host_key_alias"] == "jgstews-mac-mini.local"
    assert "known_hosts" not in kwargs, "host key verification must stay on"


def test_connect_kwargs_lowercase_keeps_trailing_dot():
    from bigfix_remote_client_relevance.transports.ssh import connect_kwargs

    kwargs = connect_kwargs(user=None, key=None, port=22, host="Host.Example.")

    assert kwargs["host_key_alias"] == "host.example."


@pytest.fixture
def captured_connect(monkeypatch):
    """Replace asyncssh.connect, recording (host, kwargs) for each call."""
    import asyncssh

    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_connect(host, **kwargs):
        calls.append((host, kwargs))
        return object()

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    return calls


async def test_connect_dials_original_host_but_verifies_lowercased(captured_connect):
    """DNS and ~/.ssh/config Host matching keep the caller's spelling; only
    the known_hosts lookup name is case-folded."""
    from bigfix_remote_client_relevance.transports.ssh import _connect

    await _connect(MIXED_CASE_HOST, None, None, 2222)

    [(host, kwargs)] = captured_connect
    assert host == MIXED_CASE_HOST
    assert kwargs["host_key_alias"] == "jgstews-mac-mini.local"
    assert kwargs["port"] == 2222
    assert "known_hosts" not in kwargs


@pytest.mark.parametrize("host", ["jgstews-mac-mini.local", "host.example.", "192.168.5.40", "::1"])
async def test_connect_leaves_already_lowercase_hosts_alone(captured_connect, host):
    """No alias when lowercasing changes nothing, so any HostKeyAlias from
    ~/.ssh/config still applies and the options are what they always were."""
    from bigfix_remote_client_relevance.transports.ssh import _connect

    await _connect(host, None, None, 22)

    assert captured_connect == [(host, {"port": 22})]


async def test_connect_without_verification_adds_no_alias(captured_connect):
    from bigfix_remote_client_relevance.transports.ssh import _connect

    await _connect(MIXED_CASE_HOST, None, None, 22, verify_host_key=False)

    assert captured_connect == [(MIXED_CASE_HOST, {"port": 22, "known_hosts": None})]


async def test_writes_nothing_to_stdout(capsys):
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    assert capsys.readouterr().out == ""


# --- Windows: the login shell is cmd.exe, not PowerShell ---------------------
#
# Every Windows command this package builds is PowerShell (Test-Path, New-Item,
# Expand-Archive, the & call operator), but Windows OpenSSH hands commands to
# cmd.exe by default. Verified on a real Server 2022 host: the discovery probe
# came back `'C:/Program was unexpected at this time.` So every Windows command
# is wrapped to run through powershell.exe, leaving the login shell alone.

WINDOWS_QNA = "C:/Program Files (x86)/BigFix Enterprise/BES Client/qna.exe"
WINDOWS_FOUND = (r"Test-Path", (f"{WINDOWS_QNA}\n", "", 0))
WINDOWS_PREREQS_OK = (r"prereq-probe", ("Expand-Archive", "", 0))


async def test_windows_commands_are_wrapped_for_powershell():
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND])

    await make_windows_transport(runner).evaluate_client_relevance("true")

    assert runner.commands(), "the transport must have run something"
    for command in runner.commands():
        assert command.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")


async def test_the_wrapped_command_decodes_to_the_powershell_source():
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND])

    await make_windows_transport(runner).evaluate_client_relevance("true")

    assert "Test-Path" in decode_ps(runner.commands()[0]), "discovery is still PowerShell inside"


async def test_progress_output_is_silenced():
    """Without this PowerShell writes CLIXML progress records to stderr."""
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND])

    await make_windows_transport(runner).evaluate_client_relevance("true")

    assert decode_ps(runner.commands()[0]).startswith("$ProgressPreference='SilentlyContinue';")


async def test_the_encoded_payload_survives_paths_with_spaces_and_parentheses():
    """cmd chokes on ( and &; base64 of UTF-16LE removes every quoting layer."""
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND])

    await make_windows_transport(runner).evaluate_client_relevance("true")

    eval_source = decode_ps(runner.commands()[-1])
    assert WINDOWS_QNA in eval_source
    assert "(x86)" in eval_source


async def test_discovery_finds_a_windows_qna_path():
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND])

    result = await make_windows_transport(runner).evaluate_client_relevance("true")

    assert result.qna_path == WINDOWS_QNA
    assert "EncodedCommand" in runner.commands()[0], "discovery must go through the wrapper"


async def test_provisioning_commands_are_wrapped_too(tmp_path):
    """provision_qna calls runner.run itself, so the wrap must be at the runner."""
    artifact = tmp_path / "QNA11.0.6.137.zip"
    artifact.write_bytes(b"fake zip")
    runner = FakeSSHRunner(
        responses=[
            (r"bfrcr-complete", ("", "", 1)),  # marker absent
            WINDOWS_PREREQS_OK,
            WINDOWS_FOUND,
        ]
    )

    await make_windows_transport(runner).evaluate_client_relevance(
        "true", qna=ResolvedQna(version="11.0.6.137", artifact_path=artifact)
    )

    sources = [decode_ps(c) for c in runner.commands()]
    assert any("Expand-Archive" in s for s in sources), "extraction must be wrapped"


async def test_put_file_is_not_wrapped(tmp_path):
    """SFTP is shell-independent — it is the one Windows step that already worked."""
    artifact = tmp_path / "QNA11.0.6.137.zip"
    artifact.write_bytes(b"fake zip")
    runner = FakeSSHRunner(
        responses=[(r"bfrcr-complete", ("", "", 1)), WINDOWS_PREREQS_OK, WINDOWS_FOUND]
    )

    await make_windows_transport(runner).evaluate_client_relevance(
        "true", qna=ResolvedQna(version="11.0.6.137", artifact_path=artifact)
    )

    assert runner.pushed, "the artifact must still be pushed"
    for _local, remote in runner.pushed:
        assert "EncodedCommand" not in remote


async def test_become_on_windows_warns(caplog):
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND])

    with caplog.at_level(logging.WARNING):
        await make_windows_transport(runner, become=True).evaluate_client_relevance("true")

    assert "sudo" not in decode_ps(runner.commands()[-1])
    assert any("become" in r.message.lower() or "sudo" in r.message.lower() for r in caplog.records)


async def test_posix_commands_are_never_wrapped():
    """The wrapper must not leak into the Linux path."""
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])

    await make_transport(runner).evaluate_client_relevance("true", qna_path="/opt/qna")

    for command in runner.commands():
        assert "powershell" not in command.lower()
        assert "EncodedCommand" not in command


# --- batches: many expressions, one qna process ------------------------------

BATCH_SENTINEL = "bfrcr-batch-test"
BATCH_MIXED = [
    "version of client",
    "(1;2)",
    "(1;2) whose (it > 5)",
    'size of file "/nonexistent"',
    "this",
    '"line1%0aQ: A: fake"',
]
FOUND_QNA = (r"command -v|if \[ -x", ("/opt/BESClient/bin/qna\n", "", 0))


@pytest.fixture
def fixed_sentinel(monkeypatch):
    from bigfix_remote_client_relevance.transports import batch

    monkeypatch.setattr(batch, "new_sentinel", lambda: BATCH_SENTINEL)


async def test_a_batch_is_one_qna_run_over_the_connection(qna_output, fixed_sentinel):
    runner = FakeSSHRunner(
        responses=[FOUND_QNA, (r"-showtypes", (qna_output("batch_mixed"), "", 0))]
    )

    results = await make_transport(runner).evaluate_client_relevance_batch(BATCH_MIXED)

    assert results is not None
    eval_calls = [c for c in runner.calls if "-showtypes" in c.command]
    assert len(eval_calls) == 1
    assert eval_calls[0].input == "".join(f'{e}\n"{BATCH_SENTINEL}"\n' for e in BATCH_MIXED)
    assert [r.client_relevance for r in results] == BATCH_MIXED
    assert results[1].answers == ["1", "2"]
    assert {(r.host, r.transport, r.qna_path, r.qna_batch_size) for r in results} == {
        ("test-host", "ssh", "/opt/BESClient/bin/qna", len(BATCH_MIXED))
    }


async def test_a_batch_that_exits_nonzero_is_refused(qna_output, fixed_sentinel):
    """Including an old qna without -showtypes: the single path learns that."""
    runner = FakeSSHRunner(
        responses=[FOUND_QNA, (r"-showtypes", ("", "unknown option -showtypes", 1))]
    )

    assert await make_transport(runner).evaluate_client_relevance_batch(BATCH_MIXED) is None


async def test_a_batch_that_does_not_line_up_is_refused(qna_output, fixed_sentinel):
    runner = FakeSSHRunner(
        responses=[FOUND_QNA, (r"-showtypes", (qna_output("batch_mixed"), "", 0))]
    )

    results = await make_transport(runner).evaluate_client_relevance_batch([*BATCH_MIXED, "x"])

    assert results is None


async def test_a_batch_with_no_qna_found_is_refused():
    runner = FakeSSHRunner(responses=[(r"command -v|if \[ -x", ("", "", 1))])

    assert await make_transport(runner).evaluate_client_relevance_batch(["a", "b"]) is None


async def test_a_batch_that_loses_the_connection_is_refused():
    async def factory():
        raise OSError("connection refused")

    transport = TransportSSH("test-host", connection_factory=factory, platform="ubuntu")

    assert await transport.evaluate_client_relevance_batch(["a", "b"]) is None


async def test_a_batch_uses_sudo_with_become(qna_output, fixed_sentinel):
    runner = FakeSSHRunner(
        responses=[FOUND_QNA, (r"-showtypes", (qna_output("batch_mixed"), "", 0))]
    )

    results = await make_transport(runner, become=True).evaluate_client_relevance_batch(BATCH_MIXED)

    assert results is not None
    assert runner.ran(r"^sudo -n /opt/BESClient/bin/qna -t -showtypes; exit \$\? # bfrcr-run-")


async def test_a_batch_without_showtypes_support_runs_plain_t(fixed_sentinel):
    """Once the single path has learned this binary predates -showtypes."""
    stdout = f"Q: A: 9.5.13.79\nT: 1\n\nQ: A: {BATCH_SENTINEL}\nT: 1\n\nQ: A: 2\nT: 1\n\nQ: A: {BATCH_SENTINEL}\nT: 1\n\nQ: "
    runner = FakeSSHRunner(responses=[FOUND_QNA, (r"qna -t;", (stdout, "", 0))])
    transport = make_transport(runner)
    transport._showtypes_supported["/opt/BESClient/bin/qna"] = False

    results = await transport.evaluate_client_relevance_batch(["version of client", "2"])

    assert results is not None
    assert [r.answers for r in results] == [["9.5.13.79"], ["2"]]


async def test_a_batch_on_windows_runs_through_powershell(qna_output, fixed_sentinel):
    """Verified live (Windows Server 2022, 11.0.x): PowerShell's `&` hands
    qna multi-line UTF-8 stdin intact, and qna answers it in CRLF."""
    crlf = qna_output("batch_mixed").replace("\n", "\r\n")
    runner = FakeSSHRunner(responses=[WINDOWS_FOUND, (r"-showtypes", (crlf, "", 0))])

    results = await make_windows_transport(runner).evaluate_client_relevance_batch(BATCH_MIXED)

    assert results is not None
    assert [r.answers for r in results][:2] == [["11.0.7.61"], ["1", "2"]]
    eval_call = next(c for c in runner.calls if "-showtypes" in decode_ps(c.command))
    assert re.search(
        rf"& '{re.escape(WINDOWS_QNA)}' -t -showtypes # bfrcr-run-[0-9a-f]{{32}}$",
        decode_ps(eval_call.command),
    )
    assert eval_call.input == "".join(f'{e}\n"{BATCH_SENTINEL}"\n' for e in BATCH_MIXED)


# --- a channel that never answers (#99) ---------------------------------------
#
# Live, a qna stdin batch to Windows Server 2022 over SSH sometimes never got
# an exit status; the single-expression fallback's new session was never
# answered either, and every hung run left an idle qna.exe on the host. These
# fakes never answer, and ignore the timeout they are handed, so only the
# transport itself can bound them.

MARKER = re.compile(r"bfrcr-run-[0-9a-f]{32}")
T = 0.2  # timeout_s for every hang test: small, but well above scheduler noise


async def _forever() -> None:
    await asyncio.Event().wait()


@dataclass
class HangingRunner(FakeSSHRunner):
    """Never answers a command matching ``hang``; answers the rest as scripted.

    ``hang_times`` limits how many matching commands hang (``None``: all).
    """

    hang: str = r"-showtypes"
    hang_times: int | None = None
    cancelled: int = 0
    hang_close: bool = False

    async def run(self, command, *, input=None, timeout=None):
        subject = decode_ps(command) if "-EncodedCommand " in command else command
        if re.search(self.hang, subject) and (self.hang_times is None or self.hang_times > 0):
            if self.hang_times is not None:
                self.hang_times -= 1
            self.calls.append(RunCall(command=command, input=input))
            try:
                await _forever()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return await super().run(command, input=input, timeout=timeout)

    async def close(self) -> None:
        self.closed = True
        if self.hang_close:
            await _forever()


def make_reconnecting_transport(*runners: FakeSSHRunner, **kwargs) -> tuple[TransportSSH, list]:
    """A transport whose n-th connection is ``runners[n]`` (the last one repeats)."""
    opened: list[FakeSSHRunner] = []

    async def factory():
        runner = runners[min(len(opened), len(runners) - 1)]
        opened.append(runner)
        return runner

    kwargs.setdefault("platform", "ubuntu")
    return TransportSSH("test-host", connection_factory=factory, **kwargs), opened


def eval_commands(runner: FakeSSHRunner) -> list[str]:
    def source(c: str) -> str:
        return decode_ps(c) if "-EncodedCommand " in c else c

    return [c.command for c in runner.calls if "-showtypes" in source(c.command)]


async def test_a_run_that_never_answers_comes_back_within_timeout_s():
    hung = HangingRunner()
    transport, _ = make_reconnecting_transport(hung, FakeSSHRunner())

    started = time.monotonic()
    result = await transport.evaluate_client_relevance("true", qna_path="/opt/qna", timeout_s=T)

    assert time.monotonic() - started < 3 * T
    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert hung.cancelled == 1, "the hung run itself is cancelled, not left pending"


async def test_a_connection_that_never_opens_comes_back_within_timeout_s():
    async def factory() -> FakeSSHRunner:
        await _forever()
        raise AssertionError("unreachable")

    transport = TransportSSH("test-host", connection_factory=factory, platform="ubuntu")

    started = time.monotonic()
    result = await transport.evaluate_client_relevance("true", qna_path="/opt/qna", timeout_s=T)

    assert time.monotonic() - started < 2 * T
    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_a_hung_eval_drops_the_connection_and_kills_its_qna_on_a_fresh_one():
    """#99: the stuck connection never answered a new session, so the kill
    goes over a new connection, and names only this run's qna."""
    hung = HangingRunner()
    fresh = FakeSSHRunner()
    transport, opened = make_reconnecting_transport(hung, fresh)

    await transport.evaluate_client_relevance("true", qna_path="/opt/qna", timeout_s=T)

    assert hung.closed, "the stuck connection is dropped"
    assert opened == [hung, fresh]
    marker = MARKER.search(eval_commands(hung)[0])
    assert marker is not None, "every qna run carries a marker the kill can find it by"
    (kill,) = fresh.commands()
    # [b]frcr: the pattern must not match the kill command's own shell.
    assert f"pgrep -f '[b]{marker.group()[1:]}'" in kill
    assert "pkill -TERM -P" in kill


async def test_the_kill_runs_under_sudo_with_become():
    hung = HangingRunner()
    fresh = FakeSSHRunner()
    transport, _ = make_reconnecting_transport(hung, fresh, become=True)

    await transport.evaluate_client_relevance("true", qna_path="/opt/qna", timeout_s=T)

    assert 'pkill -TERM -P "$p"; sudo -n pkill -TERM -P "$p"' in fresh.commands()[0]


async def test_a_hung_eval_on_windows_stops_the_process_tree_by_its_marker():
    """Closing the channel leaves qna.exe running on Windows OpenSSH: stop the
    powershell.exe whose (encoded) command line is this run's, and its tree."""
    hung = HangingRunner()
    fresh = FakeSSHRunner()
    transport, _ = make_reconnecting_transport(hung, fresh, platform="windows")

    await transport.evaluate_client_relevance("true", qna_path="C:/q/qna.exe", timeout_s=T)

    (eval_command,) = eval_commands(hung)
    assert MARKER.search(decode_ps(eval_command)), "the marker rides in the encoded source"
    (kill,) = fresh.commands()
    kill_source = decode_ps(kill)
    assert "taskkill /T /F /PID" in kill_source
    assert "Win32_Process" in kill_source
    # The only form of the marker visible in a process list is its encoding.
    token = re.search(r"-like '\*([A-Za-z0-9+/=]+)\*'", kill_source)
    assert token is not None
    token = token.group(1)
    assert eval_command.endswith(token)
    assert len(token) >= 40


async def test_every_run_gets_its_own_marker():
    runner = FakeSSHRunner(responses=[(r"-showtypes", qna_ok())])
    transport = make_transport(runner)

    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")
    await transport.evaluate_client_relevance("true", qna_path="/opt/qna")

    markers = {m.group() for c in eval_commands(runner) if (m := MARKER.search(c))}
    assert len(markers) == 2


def test_the_posix_marker_keeps_qna_exit_status(tmp_path):
    """The marker must survive in the remote shell's argv (so no exec of the
    last command) without the shell's own exit status replacing qna's."""
    import subprocess

    fake_qna = tmp_path / "qna"
    fake_qna.write_text("#!/bin/sh\nexit 3\n")
    fake_qna.chmod(0o755)
    command = TransportSSH("h", platform="ubuntu")._eval_command(
        spec_for("ubuntu"), str(fake_qna), with_types=True, marker="bfrcr-run-" + "0" * 32
    )

    assert subprocess.run(["/bin/sh", "-c", command], check=False).returncode == 3


async def test_a_timed_out_batch_is_a_warning_naming_host_and_size(caplog, fixed_sentinel):
    hung = HangingRunner(responses=[FOUND_QNA])
    transport, _ = make_reconnecting_transport(hung, FakeSSHRunner())

    with caplog.at_level(logging.WARNING):
        assert await transport.evaluate_client_relevance_batch(BATCH_MIXED, timeout_s=T) is None

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("test-host" in m and str(len(BATCH_MIXED)) in m for m in warnings), warnings


async def test_two_timeouts_in_a_row_end_the_target_for_the_rest_of_the_run():
    """After a hung batch, a hung single says the host is stuck: the rest of
    the chunk fails fast instead of costing timeout_s each (#98)."""
    hung = HangingRunner(responses=[FOUND_QNA])
    transport, _ = make_reconnecting_transport(hung)

    assert await transport.evaluate_client_relevance_batch(["a", "b"], timeout_s=T) is None
    first = await transport.evaluate_client_relevance("c", timeout_s=T)
    runs_so_far = len(hung.calls)
    started = time.monotonic()
    rest = [await transport.evaluate_client_relevance(e, timeout_s=T) for e in "defgh"]

    assert first.error_kind == ERROR_KIND_TRANSPORT
    assert time.monotonic() - started < T
    assert len(hung.calls) == runs_so_far, "nothing more is sent to a stuck host"
    assert {r.error_kind for r in rest} == {ERROR_KIND_TRANSPORT}
    assert all("did not answer" in (r.error or "") for r in rest)


async def test_an_answer_between_timeouts_keeps_the_target():
    """One slow expression is that expression's problem, not the host's."""
    hung = HangingRunner(responses=[(r"-showtypes", qna_ok())], hang_times=1)
    transport, _ = make_reconnecting_transport(hung)

    timed_out = await transport.evaluate_client_relevance("slow", qna_path="/q", timeout_s=T)
    answered = await transport.evaluate_client_relevance("fast", qna_path="/q", timeout_s=T)
    hung.hang_times = 1
    timed_out_again = await transport.evaluate_client_relevance("slow", qna_path="/q", timeout_s=T)
    still_tried = await transport.evaluate_client_relevance("fast", qna_path="/q", timeout_s=T)

    assert [r.error_kind for r in (timed_out, answered, timed_out_again, still_tried)] == [
        ERROR_KIND_TRANSPORT,
        None,
        ERROR_KIND_TRANSPORT,
        None,
    ]


async def test_a_host_that_cannot_take_the_kill_is_ended_at_once():
    """If even a fresh connection's kill goes unanswered, nothing else will be."""
    hung = HangingRunner(hang=r"-showtypes|pgrep")
    transport, _ = make_reconnecting_transport(hung)

    started = time.monotonic()
    results = [
        await transport.evaluate_client_relevance(e, qna_path="/q", timeout_s=T) for e in "abcdef"
    ]

    assert time.monotonic() - started < 3 * T
    assert {r.error_kind for r in results} == {ERROR_KIND_TRANSPORT}


async def test_evaluate_many_against_a_stuck_host_takes_about_timeout_s_not_n_times(
    fixed_sentinel,
):
    """The #99 run end to end: the first expression answers, the batch and
    everything after it hang. The caller hears back in a couple of timeouts."""
    from bigfix_remote_client_relevance.inventory import Target
    from bigfix_remote_client_relevance.orchestrate import evaluate_many

    hung = HangingRunner(responses=[FOUND_QNA], hang=r"-showtypes|pgrep")
    first_answers = FakeSSHRunner(responses=[FOUND_QNA, (r"-showtypes", qna_ok())])
    expressions = [f"e{i}" for i in range(30)]

    answered_at: list[float] = []

    class FirstThenStuck(HangingRunner):
        async def run(self, command, *, input=None, timeout=None):
            if not eval_commands(first_answers):
                result = await first_answers.run(command, input=input, timeout=timeout)
                if eval_commands(first_answers):
                    answered_at.append(time.monotonic())
                return result
            return await hung.run(command, input=input, timeout=timeout)

    transport, _ = make_reconnecting_transport(FirstThenStuck())

    results = await evaluate_many(
        expressions,
        [Target(kind="ssh", name="test-host", platform="ubuntu", arch="x86_64")],
        timeout_s=T,
        transport_factory=lambda target: transport,
    )

    # From the first answer: what comes before it is the orchestrator's own
    # first-call setup, not the hang. The batch's timeout_s, the kill's, done.
    assert time.monotonic() - answered_at[0] < 3 * T
    assert results[0].ok
    assert [r.client_relevance for r in results] == expressions
    assert {r.error_kind for r in results[1:]} == {ERROR_KIND_TRANSPORT}


async def test_aclose_is_bounded_when_the_connection_will_not_close():
    hung = HangingRunner(responses=[(r"-showtypes", qna_ok())], hang_close=True)
    transport = make_transport(hung)
    await transport.evaluate_client_relevance("true", qna_path="/q", timeout_s=T)

    started = time.monotonic()
    await transport.aclose(timeout_s=T)

    assert time.monotonic() - started < 2 * T


# --- the asyncssh adapter on a hang --------------------------------------------


class FakeProcess:
    """The slice of asyncssh's SSHClientProcess that _AsyncsshRunner uses."""

    def __init__(self, *, answer: bool) -> None:
        self._answer = answer
        self.closed = False

    async def wait(self, check=False, timeout=None):
        if not self._answer:
            await _forever()  # asyncssh 2.24.1: a timeout here never closes the channel

        class Completed:
            stdout = "A: 1\n"
            stderr = ""
            exit_status = 0

        return Completed()

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, *, open_session: bool = True, answer: bool = True) -> None:
        self._open = open_session
        self.process = FakeProcess(answer=answer)
        self.inputs: list[str | None] = []

    async def create_process(self, command, *, input=None, **kwargs):
        self.inputs.append(input)
        if not self._open:
            await _forever()  # #99's chan=4: the session request is never answered
        return self.process


async def test_adapter_runs_a_command_through_a_process():
    from bigfix_remote_client_relevance.transports.ssh import _AsyncsshRunner

    connection = FakeConnection()

    assert await _AsyncsshRunner(connection).run("qna", input="1", timeout=T) == ("A: 1\n", "", 0)
    assert connection.inputs == ["1"]


async def test_adapter_closes_the_process_when_it_times_out():
    from bigfix_remote_client_relevance.transports.ssh import _AsyncsshRunner

    connection = FakeConnection(answer=False)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _AsyncsshRunner(connection).run("qna", timeout=T)

    assert time.monotonic() - started < 2 * T
    assert connection.process.closed


async def test_adapter_bounds_a_session_that_never_opens():
    from bigfix_remote_client_relevance.transports.ssh import _AsyncsshRunner

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await _AsyncsshRunner(FakeConnection(open_session=False)).run("qna", timeout=T)

    assert time.monotonic() - started < 2 * T


async def test_adapter_closes_the_process_when_cancelled():
    from bigfix_remote_client_relevance.transports.ssh import _AsyncsshRunner

    connection = FakeConnection(answer=False)
    task = asyncio.create_task(_AsyncsshRunner(connection).run("qna", timeout=None))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.process.closed
