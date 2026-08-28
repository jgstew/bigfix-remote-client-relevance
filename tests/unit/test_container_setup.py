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

from bigfix_remote_client_relevance.transports.container_setup import docker_context_endpoint

DESKTOP_SOCKET = "unix:///Users/someone/.docker/run/docker.sock"


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
    assert docker_context_endpoint(runner_returning("ssh://user@build-box")) == "ssh://user@build-box"


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
