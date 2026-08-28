"""Shared test scaffolding: fixture loaders, fake qna binaries, marker auto-skip.

The unit suite must pass offline on a bare machine. Anything needing a real
qna binary, Docker daemon, sshd, or the network carries a marker and is
skipped automatically when the prerequisite is absent.
"""

from __future__ import annotations

import functools
import itertools
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# --- fixture loaders -------------------------------------------------------


@pytest.fixture
def qna_output():
    """Load a captured qna transcript from tests/fixtures/qna_output/."""

    def _load(name: str) -> str:
        return (FIXTURES / "qna_output" / f"{name}.txt").read_text(encoding="utf-8")

    return _load


@pytest.fixture
def release_site_fixture():
    """Load a captured support.bigfix.com page from tests/fixtures/release_site/."""

    def _load(name: str) -> str:
        return (FIXTURES / "release_site" / name).read_text(encoding="utf-8")

    return _load


# --- fake qna binaries -----------------------------------------------------

# A real executable is used rather than a mocked subprocess: the failure modes
# that matter here (stdin encoding, pipe close, timeout kill, exit codes) only
# show up when an actual process is spawned.
_FAKE_QNA_SCRIPT = '''#!/usr/bin/env python3
import pathlib
import sys
import time

here = pathlib.Path(__file__).resolve().parent
(here / "argv.txt").write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
(here / "stdin.bin").write_bytes(sys.stdin.buffer.read())
time.sleep(__SLEEP__)
sys.stdout.buffer.write((here / "stdout.bin").read_bytes())
sys.stderr.buffer.write((here / "stderr.bin").read_bytes())
sys.stdout.buffer.flush()
sys.stderr.buffer.flush()
__SENTINEL__
sys.exit(__EXIT__)
'''


@dataclass
class FakeQna:
    """A stub qna executable plus the record of how it was invoked."""

    path: str
    directory: Path

    @property
    def argv(self) -> list[str]:
        text = (self.directory / "argv.txt").read_text(encoding="utf-8")
        return text.split("\n") if text else []

    @property
    def stdin_bytes(self) -> bytes:
        return (self.directory / "stdin.bin").read_bytes()

    @property
    def stdin_text(self) -> str:
        return self.stdin_bytes.decode("utf-8", errors="replace")

    @property
    def was_invoked(self) -> bool:
        return (self.directory / "argv.txt").exists()

    @property
    def ran_to_completion(self) -> bool:
        """False when the process was killed before finishing (timeout tests)."""
        return (self.directory / "sentinel.txt").exists()


@pytest.fixture
def fake_qna(tmp_path):
    """Factory for stub qna executables.

    Each call produces an isolated executable that records its argv and stdin,
    optionally sleeps, then emits the given stdout/stderr and exit code.
    """
    counter = itertools.count()

    def _make(
        *,
        stdout: str = "",
        stdout_bytes: bytes | None = None,
        stderr: str = "",
        exit_code: int = 0,
        sleep: float = 0.0,
    ) -> FakeQna:
        directory = tmp_path / f"fake_qna_{next(counter)}"
        directory.mkdir()
        payload = stdout_bytes if stdout_bytes is not None else stdout.encode("utf-8")
        (directory / "stdout.bin").write_bytes(payload)
        (directory / "stderr.bin").write_bytes(stderr.encode("utf-8"))

        script = (
            _FAKE_QNA_SCRIPT.replace("__SLEEP__", repr(float(sleep)))
            .replace("__SENTINEL__", '(here / "sentinel.txt").write_text("done")')
            .replace("__EXIT__", repr(int(exit_code)))
        )
        # The script always lands beside its recordings, so it can find them
        # through __file__ under either layout below.
        (directory / "qna.py").write_text(script, encoding="utf-8")

        if sys.platform.startswith("win"):
            # A shebang means nothing to CreateProcess, so the stub has to be
            # something Windows can spawn. A .cmd is: CreateProcess runs it
            # through cmd.exe, which forwards argv, passes the stdin pipe
            # straight down, and returns the child's exit code.
            path = directory / "qna.cmd"
            path.write_text(f'@"{sys.executable}" "%~dp0qna.py" %*\n', encoding="utf-8")
        else:
            path = directory / "qna"
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        return FakeQna(path=str(path), directory=directory)

    return _make


# --- fake sudo -------------------------------------------------------------

# Real sudo cannot run here: it would prompt, or be absent on a CI runner. A
# stub on PATH proves what argv assertions cannot -- that the relevance
# expression still reaches qna's stdin *through* the extra exec.
_FAKE_SUDO_SCRIPT = '''#!/usr/bin/env python3
import os
import pathlib
import sys

here = pathlib.Path(__file__).resolve().parent
# Appended, not overwritten: a caching test needs to prove a *second* call
# never re-invoked sudo, which an overwritten record could not show.
with open(here / "calls.log", "a", encoding="utf-8") as log:
    log.write("\\t".join(sys.argv[1:]) + "\\n")

deny = here / "deny.bin"
if deny.exists():
    sys.stderr.buffer.write(deny.read_bytes())
    sys.stderr.buffer.flush()
    sys.exit(1)

# Drop sudo's own options; what remains is the command real sudo would run.
rest = list(sys.argv[1:])
while rest and rest[0].startswith("-"):
    rest.pop(0)
if not rest:
    sys.stderr.write("sudo: no command specified\\n")
    sys.exit(1)
# execv, not a subprocess: the command must inherit this process's stdin pipe.
os.execv(rest[0], rest)
'''


@dataclass
class FakeSudo:
    """A stub sudo on PATH plus the record of how it was invoked."""

    directory: Path

    @property
    def _calls(self) -> list[str]:
        log = self.directory / "calls.log"
        if not log.exists():
            return []
        return [line for line in log.read_text(encoding="utf-8").splitlines() if line]

    @property
    def argv(self) -> list[str]:
        """The most recent invocation's argv."""
        calls = self._calls
        return calls[-1].split("\t") if calls else []

    @property
    def was_invoked(self) -> bool:
        return bool(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)


@pytest.fixture
def fake_sudo(tmp_path, monkeypatch):
    """Factory for a stub ``sudo`` at the front of PATH.

    The default passes the command through so a `fake_qna` behind it still
    runs; ``deny`` makes it refuse the way `sudo -n` does without a usable
    credential -- stderr, exit 1, and the command never executed.
    """
    counter = itertools.count()

    def _make(*, deny: str | None = None) -> FakeSudo:
        directory = tmp_path / f"fake_sudo_{next(counter)}"
        directory.mkdir()
        if deny is not None:
            (directory / "deny.bin").write_bytes(deny.encode("utf-8"))

        path = directory / "sudo"
        path.write_text(_FAKE_SUDO_SCRIPT, encoding="utf-8")
        path.chmod(0o755)
        monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
        return FakeSudo(directory=directory)

    return _make


@pytest.fixture
def allow_non_root_macos(monkeypatch):
    """Neutralize the macOS root check so subprocess behavior can be tested.

    qna itself needs root for some inspectors on macOS; the stub binaries do
    not, and the check has its own dedicated tests.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)


# --- marker auto-skip ------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _live_qna_status() -> tuple[bool, str]:
    try:
        from bigfix_remote_client_relevance.qna_paths import find_qna_path
    except ImportError:  # pragma: no cover - only before the package exists
        return False, "bigfix_remote_client_relevance is not importable yet"
    path = find_qna_path()
    if path is None:
        return False, "no local qna binary found"
    if sys.platform == "darwin" and os.geteuid() != 0:
        return False, "qna requires root on macOS - rerun under: sudo -E uv run pytest -m live_qna"
    return True, ""


@functools.lru_cache(maxsize=1)
def _docker_status() -> tuple[bool, str]:
    # Uses the package's own socket discovery rather than docker.from_env(), so
    # the probe agrees with what DockerEngine will actually connect to.
    try:
        from bigfix_remote_client_relevance.transports.container import DockerEngine
    except ImportError:  # pragma: no cover - only before the package exists
        return False, "bigfix_remote_client_relevance is not importable yet"
    try:
        # auto_setup=False is load-bearing: this runs at collection time, and
        # a dev box with Docker stopped would otherwise launch Docker Desktop
        # and block the whole suite for the start timeout.
        client = DockerEngine(auto_setup=False)._get_client()
        os_type = client.info().get("OSType")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - any failure at all means no usable daemon
        return False, "no reachable Docker daemon"

    # Reachable is not enough: every image these tests build is a Linux one,
    # and a daemon in Windows-container mode rejects them at the manifest.
    if os_type != "linux":
        return False, (
            f"the Docker daemon runs {os_type or 'unknown'} containers; "
            "these tests build Linux images"
        )
    return True, ""


@functools.lru_cache(maxsize=1)
def _ssh_localhost_status() -> tuple[bool, str]:
    """Can the SSH transport actually log into localhost?

    Reports *why* not, because the three failure modes need different fixes:
    sshd not running, the host key not trusted, or — much the most common on a
    dev box — sshd running fine with no key authorized for this user.
    """
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "localhost", "true"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode == 0:
        return True, ""

    stderr = completed.stderr.strip()
    lowered = stderr.lower()
    if "permission denied" in lowered:
        return False, (
            "sshd on localhost is running but no key is authorized for this user. "
            "To enable: ssh-keygen -t ed25519 (if needed), then "
            "cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys"
        )
    if "host key verification" in lowered:
        return False, (
            "localhost's host key is not trusted; add it with "
            "ssh-keyscan -H localhost >> ~/.ssh/known_hosts"
        )
    if "connection refused" in lowered or "connect to host" in lowered:
        return False, "sshd is not accepting connections on localhost (enable Remote Login)"
    return False, f"cannot ssh to localhost: {stderr or 'unknown failure'}"


@functools.lru_cache(maxsize=1)
def _ssh_windows_status() -> tuple[bool, str]:
    """Can we reach the Windows host named by BFRCR_WINDOWS_SSH_HOST?

    Opt-in by design: it needs a real Windows box, and the point of these tests
    is the shell the box actually logs you into.
    """
    host = os.environ.get("BFRCR_WINDOWS_SSH_HOST")
    if not host:
        return False, (
            "set BFRCR_WINDOWS_SSH_HOST=user@host to run the Windows SSH tests "
            "against a real Windows endpoint"
        )
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "echo ok"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return True, ""
    return False, f"cannot ssh to {host}: {completed.stderr.strip() or 'unknown failure'}"


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "live_qna" in item.keywords:
            ok, reason = _live_qna_status()
            if not ok:
                item.add_marker(pytest.mark.skip(reason=reason))
        if "docker" in item.keywords:
            ok, reason = _docker_status()
            if not ok:
                item.add_marker(pytest.mark.skip(reason=reason))
        if "ssh_localhost" in item.keywords:
            ok, reason = _ssh_localhost_status()
            if not ok:
                item.add_marker(pytest.mark.skip(reason=reason))
        if "ssh_windows" in item.keywords:
            ok, reason = _ssh_windows_status()
            if not ok:
                item.add_marker(pytest.mark.skip(reason=reason))
        if "network" in item.keywords and os.environ.get("BFRCR_NETWORK_TESTS") != "1":
            item.add_marker(
                pytest.mark.skip(reason="network tests are opt-in: set BFRCR_NETWORK_TESTS=1")
            )
