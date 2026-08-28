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

logger = logging.getLogger(__name__)

Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]

_CONTEXT_ARGV = ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
_CONTEXT_TIMEOUT_S = 5.0

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


__all__ = ["docker_context_endpoint"]
