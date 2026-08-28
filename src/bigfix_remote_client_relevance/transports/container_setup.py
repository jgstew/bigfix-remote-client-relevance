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
    raise NotImplementedError


__all__ = ["docker_context_endpoint"]
