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
        path = directory / "qna"
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
        return FakeQna(path=str(path), directory=directory)

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
def _docker_available() -> bool:
    # Uses the package's own socket discovery rather than docker.from_env(), so
    # the probe agrees with what DockerEngine will actually connect to.
    try:
        from bigfix_remote_client_relevance.transports.container import DockerEngine
    except ImportError:  # pragma: no cover - only before the package exists
        return False
    try:
        DockerEngine()._get_client()
        return True
    except Exception:  # noqa: BLE001 - any failure at all means no usable daemon
        return False


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
        if "docker" in item.keywords and not _docker_available():
            item.add_marker(pytest.mark.skip(reason="no reachable Docker daemon"))
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
