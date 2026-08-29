"""Tests for finding a container engine the docker SDK would miss.

The SDK reads DOCKER_HOST and otherwise assumes /var/run/docker.sock; it does
not read Docker contexts, which is where Docker Desktop, Colima and Rancher
Desktop actually record their sockets. Reading the context closes that gap —
but a probe of the host has many ways to fail, and none of them should be an
error, so most of these tests are about failing quietly.
"""

from __future__ import annotations

import subprocess

import pytest

from bigfix_remote_client_relevance.transports.container_setup import (
    docker_context_endpoint,
    podman_context_endpoint,
)

DESKTOP_SOCKET = "unix:///Users/someone/.docker/run/docker.sock"
PODMAN_SOCKET_PATH = "/run/user/1000/podman/podman.sock"


def runner_returning(stdout: str = "", *, returncode: int = 0):
    calls: list[tuple[list[str], float]] = []

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def runner_raising(exc: Exception):
    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise exc

    return run


def test_reads_the_endpoint_from_the_current_context():
    assert docker_context_endpoint(runner_returning(f"{DESKTOP_SOCKET}\n")) == DESKTOP_SOCKET


def test_a_remote_tcp_endpoint_is_kept():
    """The SDK can dial these, and they are exactly what the hardcoded list misses."""
    assert docker_context_endpoint(runner_returning("tcp://10.0.0.5:2375")) == "tcp://10.0.0.5:2375"


def test_an_ssh_endpoint_is_kept():
    assert (
        docker_context_endpoint(runner_returning("ssh://user@build-box")) == "ssh://user@build-box"
    )


def test_a_missing_docker_binary_is_not_an_error():
    assert docker_context_endpoint(runner_raising(FileNotFoundError("docker"))) is None


def test_a_hang_is_not_an_error():
    timeout = subprocess.TimeoutExpired(["docker"], 5)

    assert docker_context_endpoint(runner_raising(timeout)) is None


def test_a_failing_command_is_not_an_error():
    assert docker_context_endpoint(runner_returning("", returncode=1)) is None


@pytest.mark.parametrize("stdout", ["", "   \n", "<no value>"])
def test_empty_and_placeholder_output_are_ignored(stdout):
    assert docker_context_endpoint(runner_returning(stdout)) is None


def test_an_unusable_scheme_is_ignored():
    """Better the hardcoded fallbacks than a scheme the SDK cannot dial."""
    assert docker_context_endpoint(runner_returning("garbage-not-a-url")) is None


def test_the_probe_is_bounded():
    """A wedged docker CLI must not hang the whole run."""
    run = runner_returning(DESKTOP_SOCKET)

    docker_context_endpoint(run, timeout=2.5)

    assert len(run.calls) == 1, "the probe must actually have run"
    _argv, timeout = run.calls[0]
    assert timeout == 2.5


def test_the_probe_asks_for_the_docker_endpoint():
    run = runner_returning(DESKTOP_SOCKET)

    docker_context_endpoint(run)

    argv, _timeout = run.calls[0]
    assert argv[:2] == ["context", "inspect"]
    assert "{{.Endpoints.docker.Host}}" in argv


# --- an engine that is installed but not running ------------------------------
#
# "is Docker running?" is a question the tool can answer for itself. Detection
# is injectable so no test ever launches anything.


def fake_host(*, installed: tuple[str, ...] = (), apps: tuple[str, ...] = ()):
    """A machine with the named binaries on PATH and the named apps installed."""
    return {
        "which": lambda name: f"/usr/local/bin/{name}" if name in installed else None,
        "exists": lambda path: any(app in path for app in apps),
    }


def test_docker_desktop_is_detected_on_macos():
    from bigfix_remote_client_relevance.transports.container_setup import detect_engine_starter

    starter = detect_engine_starter(system="darwin", **fake_host(apps=("Docker.app",)))

    assert starter is not None
    assert starter.argv == ["open", "-a", "Docker"]


def test_colima_is_detected_when_docker_desktop_is_absent():
    from bigfix_remote_client_relevance.transports.container_setup import detect_engine_starter

    starter = detect_engine_starter(system="darwin", **fake_host(installed=("colima",)))

    assert starter is not None
    assert starter.argv == ["colima", "start"]


def test_nothing_installed_is_detected_as_nothing():
    from bigfix_remote_client_relevance.transports.container_setup import detect_engine_starter

    assert detect_engine_starter(system="darwin", **fake_host()) is None


def test_linux_reports_the_command_rather_than_running_it():
    """Starting a system service needs privileges this tool should not exercise."""
    from bigfix_remote_client_relevance.transports.container_setup import detect_engine_starter

    starter = detect_engine_starter(system="linux", **fake_host(installed=("docker",)))

    assert starter is not None
    assert starter.argv is None, "a system daemon is not ours to start"
    assert "systemctl" in starter.note


def test_the_macos_install_hint_names_a_real_command():
    from bigfix_remote_client_relevance.transports.container_setup import install_hint

    assert "brew install" in install_hint("darwin")


def test_the_linux_install_hint_points_at_the_docs():
    """No invented distro commands — the docs are the honest answer."""
    from bigfix_remote_client_relevance.transports.container_setup import install_hint

    hint = install_hint("linux")

    assert "docs.docker.com" in hint


# --- podman: the same probes, but podman's own commands and output shape ------


def test_podman_reads_the_socket_path_from_info():
    """podman info reports a bare filesystem path, not a URL like docker context."""
    assert (
        podman_context_endpoint(runner_returning(f"{PODMAN_SOCKET_PATH}\n"))
        == f"unix://{PODMAN_SOCKET_PATH}"
    )


def test_a_missing_podman_binary_is_not_an_error():
    assert podman_context_endpoint(runner_raising(FileNotFoundError("podman"))) is None


def test_a_podman_hang_is_not_an_error():
    timeout = subprocess.TimeoutExpired(["podman"], 5)

    assert podman_context_endpoint(runner_raising(timeout)) is None


def test_a_failing_podman_command_is_not_an_error():
    assert podman_context_endpoint(runner_returning("", returncode=1)) is None


@pytest.mark.parametrize("stdout", ["", "   \n"])
def test_empty_podman_output_is_ignored(stdout):
    assert podman_context_endpoint(runner_returning(stdout)) is None


def test_the_podman_probe_asks_podman_info():
    run = runner_returning(PODMAN_SOCKET_PATH)

    podman_context_endpoint(run)

    argv, _timeout = run.calls[0]
    assert argv[0] == "info"
    assert "{{.Host.RemoteSocket.Path}}" in argv


# --- podman: engine-starter detection, never deferring to docker/Colima -------


def test_podman_machine_is_detected_on_macos():
    from bigfix_remote_client_relevance.transports.container_setup import detect_podman_starter

    starter = detect_podman_starter(system="darwin", **fake_host(installed=("podman",)))

    assert starter is not None
    assert starter.argv == ["podman", "machine", "start"]


def test_podman_starter_ignores_docker_and_colima_on_macos():
    """A PodmanEngine explicitly asked for must never fall back to Docker Desktop."""
    from bigfix_remote_client_relevance.transports.container_setup import detect_podman_starter

    starter = detect_podman_starter(
        system="darwin", **fake_host(installed=("podman", "colima"), apps=("Docker.app",))
    )

    assert starter is not None
    assert starter.argv == ["podman", "machine", "start"]


def test_podman_is_reported_rather_than_started_on_linux():
    from bigfix_remote_client_relevance.transports.container_setup import detect_podman_starter

    starter = detect_podman_starter(system="linux", **fake_host(installed=("podman",)))

    assert starter is not None
    assert starter.argv is None, "a system service is not ours to start"
    assert "podman.socket" in starter.note


def test_detect_podman_starter_ignores_docker_on_linux():
    from bigfix_remote_client_relevance.transports.container_setup import detect_podman_starter

    starter = detect_podman_starter(system="linux", **fake_host(installed=("docker", "podman")))

    assert starter is not None
    assert starter.name == "podman"


def test_nothing_installed_is_detected_as_no_podman_starter():
    from bigfix_remote_client_relevance.transports.container_setup import detect_podman_starter

    assert detect_podman_starter(system="darwin", **fake_host()) is None


def test_the_podman_install_hint_names_a_real_command():
    from bigfix_remote_client_relevance.transports.container_setup import install_hint

    assert "brew install podman" in install_hint("darwin", engine="podman")


def test_the_docker_install_hint_is_unchanged_by_the_new_parameter():
    from bigfix_remote_client_relevance.transports.container_setup import install_hint

    assert "brew install --cask docker" in install_hint("darwin")
    assert "brew install --cask docker" in install_hint("darwin", engine="docker")


def test_podman_engine_setup_detects_and_hints_podman_only():
    from bigfix_remote_client_relevance.transports.container_setup import PodmanEngineSetup

    setup = PodmanEngineSetup()

    # detect() must go through detect_podman_starter, not detect_engine_starter,
    # so it never reports "start Docker Desktop" for a user who asked for podman.
    import bigfix_remote_client_relevance.transports.container_setup as mod

    real_detect = mod.detect_podman_starter
    calls = []
    mod.detect_podman_starter = lambda **kw: (calls.append(kw), real_detect(**kw))[1]
    try:
        setup.detect()
    finally:
        mod.detect_podman_starter = real_detect
    assert calls, "PodmanEngineSetup.detect() must call detect_podman_starter"

    assert "podman" in setup.hint()
