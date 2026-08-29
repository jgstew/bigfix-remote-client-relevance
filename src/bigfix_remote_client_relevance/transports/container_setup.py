"""Finding a container engine the docker SDK would not find on its own.

``docker.from_env()`` honours ``DOCKER_HOST`` and otherwise assumes
``/var/run/docker.sock``. It does not read Docker *contexts*, which is how
Docker Desktop, Colima, Rancher Desktop and remote engines actually record
where they listen — so the SDK misses setups the ``docker`` CLI finds without
trouble.

Everything here probes the host and is therefore injectable: the callers pass
their own runner in tests, and nothing raises. A probe that cannot answer is
not an error, it just means falling back to the hardcoded list of socket paths.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]

_CONTEXT_ARGV = ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
_CONTEXT_TIMEOUT_S = 5.0
_PODMAN_INFO_ARGV = ["info", "--format", "{{.Host.RemoteSocket.Path}}"]

# Schemes the docker SDK can dial. A context naming anything else is skipped
# rather than handed over to fail obscurely later.
_SUPPORTED_SCHEMES = ("unix://", "tcp://", "ssh://", "npipe://")


def _run_docker(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    import shutil

    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError("docker")
    # Fixed argv, no shell: nothing here interpolates user input.
    return subprocess.run(
        [docker, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def docker_context_endpoint(
    runner: Runner | None = None, *, timeout: float = _CONTEXT_TIMEOUT_S
) -> str | None:
    """The current Docker context's endpoint, or ``None``.

    Never raises. A missing ``docker`` binary, a non-zero exit, a hang, empty
    output, or a scheme we cannot dial all mean the same thing to the caller:
    fall back to the hardcoded socket list.
    """
    run = runner or _run_docker
    try:
        completed = run(_CONTEXT_ARGV, timeout)
    except Exception as exc:  # noqa: BLE001 - every failure is just "no answer"
        logger.debug("could not read the docker context: %s", exc)
        return None

    if completed.returncode != 0:
        logger.debug("docker context inspect exited %d", completed.returncode)
        return None

    endpoint = completed.stdout.strip().splitlines()[0].strip() if completed.stdout.strip() else ""
    # Go templates render a missing field as this rather than failing.
    if not endpoint or endpoint == "<no value>":
        return None
    if not endpoint.startswith(_SUPPORTED_SCHEMES):
        logger.debug("ignoring docker context endpoint %r: unsupported scheme", endpoint)
        return None

    logger.debug("docker context endpoint: %s", endpoint)
    return endpoint


def _run_podman(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    import shutil

    podman = shutil.which("podman")
    if podman is None:
        raise FileNotFoundError("podman")
    # Fixed argv, no shell: nothing here interpolates user input.
    return subprocess.run(
        [podman, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def podman_context_endpoint(
    runner: Runner | None = None, *, timeout: float = _CONTEXT_TIMEOUT_S
) -> str | None:
    """The socket podman's current connection actually uses, or ``None``.

    ``podman info`` reports a bare filesystem path rather than a URL the way
    ``docker context inspect`` does, so this wraps it as ``unix://<path>``.
    Never raises: a missing ``podman`` binary, a non-zero exit, a hang, or
    empty output all mean the same thing to the caller — fall back to the
    hardcoded socket list.
    """
    run = runner or _run_podman
    try:
        completed = run(_PODMAN_INFO_ARGV, timeout)
    except Exception as exc:  # noqa: BLE001 - every failure is just "no answer"
        logger.debug("could not read the podman socket path: %s", exc)
        return None

    if completed.returncode != 0:
        logger.debug("podman info exited %d", completed.returncode)
        return None

    path = completed.stdout.strip().splitlines()[0].strip() if completed.stdout.strip() else ""
    if not path:
        return None

    endpoint = f"unix://{path}"
    logger.debug("podman socket endpoint: %s", endpoint)
    return endpoint


@dataclass(frozen=True)
class EngineStarter:
    """An engine found on this machine, and how (or whether) to start it."""

    name: str
    argv: list[str] | None
    """``None`` means detected but not ours to start — see ``note``."""

    note: str = ""


_MACOS_APPS = (
    ("Docker Desktop", "/Applications/Docker.app", ["open", "-a", "Docker"]),
    ("Rancher Desktop", "/Applications/Rancher Desktop.app", ["open", "-a", "Rancher Desktop"]),
)


def detect_engine_starter(
    *,
    system: str | None = None,
    which: Callable[[str], str | None] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> EngineStarter | None:
    """The engine installed here that could be started, if any.

    Linux and Windows are detected but never started: starting a system
    service needs privileges this tool should not be exercising on the user's
    behalf, so they are reported with the command to run instead.
    """
    import os.path
    import shutil
    import sys

    system = system if system is not None else sys.platform
    which = which if which is not None else shutil.which
    exists = exists if exists is not None else os.path.exists

    if system == "darwin":
        for name, app, argv in _MACOS_APPS:
            if exists(app):
                return EngineStarter(name=name, argv=argv)
        if which("colima"):
            return EngineStarter(name="Colima", argv=["colima", "start"])
        # Ordered last: podman only helps here when its machine exposes a
        # docker-compatible socket that DOCKER_HOST or a context names.
        if which("podman"):
            return EngineStarter(name="podman machine", argv=["podman", "machine", "start"])
        return None

    if system.startswith("linux"):
        if which("docker"):
            return EngineStarter(
                name="Docker",
                argv=None,
                note="start it with: sudo systemctl start docker",
            )
        if which("podman"):
            return EngineStarter(
                name="podman",
                argv=None,
                note="start it with: systemctl --user start podman.socket",
            )
        return None

    if system.startswith("win"):
        for app in (
            r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
            r"C:\Program Files\Rancher Desktop\Rancher Desktop.exe",
        ):
            if exists(app):
                return EngineStarter(
                    name="Docker Desktop",
                    argv=None,
                    note="start Docker Desktop from the Start menu",
                )
        return None

    return None


def detect_podman_starter(
    *,
    system: str | None = None,
    which: Callable[[str], str | None] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> EngineStarter | None:
    """The podman installed here that could be started, if any.

    Unlike :func:`detect_engine_starter`, this never prefers Docker Desktop
    or Colima when both are present: a caller reaching for this function has
    already decided it wants podman specifically, so it must not silently
    start (or report) a different engine instead.
    """
    import shutil
    import sys

    system = system if system is not None else sys.platform
    which = which if which is not None else shutil.which

    if system == "darwin" or system.startswith("win"):
        if which("podman"):
            return EngineStarter(name="podman machine", argv=["podman", "machine", "start"])
        return None

    if system.startswith("linux"):
        if which("podman"):
            return EngineStarter(
                name="podman",
                argv=None,
                note="start it with: systemctl --user start podman.socket",
            )
        return None

    return None


def install_hint(system: str | None = None, *, engine: str = "docker") -> str:
    """How to install a container engine on this platform.

    Only commands that are actually right for the platform — a wrong install
    command is worse than none, which is why Linux and Windows get the docs
    rather than a guessed package manager.
    """
    import sys

    system = system if system is not None else sys.platform
    if engine == "podman":
        if system == "darwin":
            return "install one with: brew install podman"
        if system.startswith("win"):
            return "install podman: https://podman.io/docs/installation#windows"
        return "install podman: https://podman.io/docs/installation#installing-on-linux"
    if system == "darwin":
        return (
            "install one with: brew install --cask docker  (Docker Desktop), "
            "or: brew install colima docker  (Colima)"
        )
    if system.startswith("win"):
        return "install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/"
    return "install Docker Engine: https://docs.docker.com/engine/install/"


class EngineSetup:
    """Everything that touches the machine, behind one injectable seam.

    Real implementations launch applications and block; tests substitute this
    wholesale so nothing is ever started or waited for.
    """

    def detect(self) -> EngineStarter | None:
        return detect_engine_starter()

    def start(self, starter: EngineStarter) -> None:
        if starter.argv is None:  # pragma: no cover - callers check first
            raise ValueError(f"{starter.name} is not ours to start")
        subprocess.run(starter.argv, capture_output=True, text=True, check=False)

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def hint(self) -> str:
        return install_hint()


class PodmanEngineSetup(EngineSetup):
    """Like :class:`EngineSetup`, but only ever detects or hints at podman."""

    def detect(self) -> EngineStarter | None:
        return detect_podman_starter()

    def hint(self) -> str:
        return install_hint(engine="podman")


__all__ = [
    "EngineSetup",
    "EngineStarter",
    "PodmanEngineSetup",
    "detect_engine_starter",
    "detect_podman_starter",
    "docker_context_endpoint",
    "install_hint",
    "podman_context_endpoint",
]
