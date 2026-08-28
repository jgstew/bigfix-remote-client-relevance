"""Evaluate client relevance inside a container.

Covers the on-demand Linux long tail — "does this answer correctly on Ubuntu
22.04 / RHEL 9 / Amazon Linux 2023?" — with no SSH credentials and no
long-lived VM per distro. It complements rather than replaces SSH, which stays
the answer for macOS and Windows since neither runs meaningfully in a container.

The docker SDK sits behind :class:`ContainerEngine` so another OCI engine
(podman) can slot in, and so the transport is testable without a daemon.

Artifacts are bind-mounted read-only rather than copied in: the engine is
local, so there is nothing to gain from a copy, and a read-only mount means a
container cannot corrupt the controller's cache.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypeVar

from bigfix_remote_client_relevance.bootstrap.provision import (
    BootstrapFailure,
    RunResult,
    provision_qna,
    qna_path_for,
)
from bigfix_remote_client_relevance.bootstrap.targets import (
    TargetSpec,
    UnknownTargetError,
    classify_uname,
    spec_for,
)
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
    parse_qna_output,
)
from bigfix_remote_client_relevance.transports.local import (
    QNA_EVAL_FLAGS,
    classify_qna_outcome,
    normalize_stdin_payload,
)

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "container"

T = TypeVar("T")

ARTIFACT_MOUNT = "/bigfix_qna_artifacts"
"""Where the controller's cached artifact appears inside the container."""

DEFAULT_QNA_COMMAND = "qna"

# A container that must stay alive between commands needs a foreground process.
KEEP_ALIVE_COMMAND = "sleep infinity"

# Same probe TransportSSH runs: kernel on line one, os-release tokens on line
# two. The container's answer is authoritative, so it is classified strictly.
PLATFORM_PROBE_COMMAND = 'uname -s; . /etc/os-release 2>/dev/null && echo "$ID $ID_LIKE"'

# Which package manager does the image actually carry? Cheap cross-check for
# an explicit (human-supplied) platform; ends in true so absence is not an error.
FAMILY_SANITY_COMMAND = (
    "command -v dpkg >/dev/null 2>&1 && echo dpkg; "
    "command -v rpm >/dev/null 2>&1 && echo rpm; true"
)

_SPEC_FAMILIES = {"ubuntu": "deb", "debian": "deb", "rhel": "rpm", "suse": "rpm"}


class ContainerEngineError(Exception):
    """The container engine is unreachable or refused an operation."""


class ContainerEngine(Protocol):
    """The slice of a container engine this transport uses."""

    async def ensure_image(self, image: str, *, platform: str | None = None) -> None: ...

    async def run_one_shot(
        self,
        image: str,
        command: str,
        *,
        input: str | None = None,
        mounts: dict[str, str] | None = None,
        platform: str | None = None,
        timeout: float | None = None,
    ) -> RunResult: ...

    async def start(
        self,
        image: str,
        *,
        mounts: dict[str, str] | None = None,
        platform: str | None = None,
    ) -> str: ...

    async def exec_in(
        self,
        container_id: str,
        command: str,
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> RunResult: ...

    async def stop(self, container_id: str) -> None: ...


def candidate_docker_sockets() -> list[str]:
    """Socket URLs to try, most specific first.

    ``docker.from_env()`` honours ``DOCKER_HOST`` and otherwise assumes
    ``/var/run/docker.sock``. Docker Desktop does create that path, but Docker
    *contexts* are not read by the SDK, so setups that only listen elsewhere —
    Colima, Rancher Desktop, or a Desktop install with the compatibility
    symlink disabled — are unreachable through it. Trying each known location
    covers those, and reports every path tried when none answers.
    """
    candidates: list[str] = []
    from_env = os.environ.get("DOCKER_HOST")
    if from_env:
        candidates.append(from_env)
    candidates.append(f"unix://{Path.home() / '.docker/run/docker.sock'}")
    candidates.append("unix:///var/run/docker.sock")
    # Colima and Rancher Desktop keep their sockets elsewhere again.
    candidates.append(f"unix://{Path.home() / '.colima/default/docker.sock'}")
    candidates.append(f"unix://{Path.home() / '.rd/docker.sock'}")
    return list(dict.fromkeys(candidates))


class DockerEngine:
    """:class:`ContainerEngine` backed by the docker SDK.

    The SDK is blocking, so every call is pushed to a worker thread.
    """

    def __init__(
        self, client: object | None = None, *, socket_candidates: list[str] | None = None
    ) -> None:
        self._client = client
        self._socket_candidates = socket_candidates

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client

        import docker

        tried: list[str] = []
        for url in self._socket_candidates or candidate_docker_sockets():
            tried.append(url)
            try:
                client = docker.DockerClient(base_url=url)
                client.ping()
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                logger.debug("no container engine at %s: %s", url, exc)
                continue
            logger.debug("connected to the container engine at %s", url)
            self._client = client
            return client

        raise ContainerEngineError(
            "cannot connect to the Docker daemon; is it running? tried: " + ", ".join(tried)
        )

    async def ensure_image(self, image: str, *, platform: str | None = None) -> None:
        def _ensure() -> None:
            import docker.errors

            client = self._get_client()
            try:
                client.images.get(image)  # type: ignore[attr-defined]
            except docker.errors.ImageNotFound:
                logger.info("pulling image %s", image)
                client.images.pull(image, platform=platform)  # type: ignore[attr-defined]

        await self._guard(_ensure)

    async def run_one_shot(
        self,
        image: str,
        command: str,
        *,
        input: str | None = None,
        mounts: dict[str, str] | None = None,
        platform: str | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        # The SDK's containers.run() cannot feed stdin, so the container is
        # created, started, attached to, and removed explicitly.
        container_id = await self.start(image, mounts=mounts, platform=platform)
        try:
            return await self.exec_in(container_id, command, input=input, timeout=timeout)
        finally:
            await self.stop(container_id)

    async def start(
        self,
        image: str,
        *,
        mounts: dict[str, str] | None = None,
        platform: str | None = None,
    ) -> str:
        def _start() -> str:
            client = self._get_client()
            container = client.containers.run(  # type: ignore[attr-defined]
                image,
                command=KEEP_ALIVE_COMMAND,
                detach=True,
                volumes=_to_volumes(mounts or {}),
                platform=platform,
                auto_remove=False,
            )
            return str(container.id)

        return await self._guard(_start)

    async def exec_in(
        self,
        container_id: str,
        command: str,
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        def _exec() -> RunResult:
            client = self._get_client()
            container = client.containers.get(container_id)  # type: ignore[attr-defined]
            if input is None:
                result = container.exec_run(["sh", "-lc", command], demux=True)
                stdout, stderr = result.output
                return (
                    (stdout or b"").decode("utf-8", errors="replace"),
                    (stderr or b"").decode("utf-8", errors="replace"),
                    int(result.exit_code or 0),
                )
            # Feeding stdin through the SDK means driving the raw exec socket,
            # which is brittle. Writing the payload to a file and redirecting it
            # in achieves the same thing with plain shell. `printf '%s'` keeps
            # the format string separate from the data, so a client relevance
            # containing % or backslashes is passed through untouched.
            handle = _stdin_path(container_id)
            stage = container.exec_run(
                ["sh", "-lc", f"printf '%s' {shlex.quote(input)} > {handle}"], demux=True
            )
            if stage.exit_code not in (0, None):
                raise ContainerEngineError("could not stage stdin inside the container")
            result = container.exec_run(["sh", "-lc", f"{command} < {handle}"], demux=True)
            stdout, stderr = result.output
            return (
                (stdout or b"").decode("utf-8", errors="replace"),
                (stderr or b"").decode("utf-8", errors="replace"),
                int(result.exit_code or 0),
            )

        return await asyncio.wait_for(self._guard(_exec), timeout=timeout)

    async def stop(self, container_id: str) -> None:
        def _stop() -> None:
            client = self._get_client()
            container = client.containers.get(container_id)  # type: ignore[attr-defined]
            container.remove(force=True)

        try:
            await self._guard(_stop)
        except ContainerEngineError:
            logger.warning("could not remove container %s", container_id)

    @staticmethod
    async def _guard(fn: Callable[[], T]) -> T:
        """Run a blocking SDK call off the event loop, normalizing its errors."""
        try:
            return await asyncio.to_thread(fn)
        except ContainerEngineError:
            raise
        except Exception as exc:
            raise ContainerEngineError(str(exc)) from exc


def _to_volumes(mounts: dict[str, str]) -> dict[str, dict[str, str]]:
    """Convert ``{host: "/target:ro"}`` into the SDK's volume mapping."""
    volumes: dict[str, dict[str, str]] = {}
    for source, target in mounts.items():
        bind, _, mode = target.partition(":")
        volumes[source] = {"bind": bind, "mode": mode or "rw"}
    return volumes


def _stdin_path(container_id: str) -> str:
    return f"/tmp/.bfrcr-stdin-{container_id[:12]}"


class _ContainerRunner:
    """Adapts a running container to the provisioning CommandRunner protocol.

    ``put_file`` is a copy from the read-only mount rather than a transfer:
    the artifact is already visible inside the container.
    """

    def __init__(self, engine: ContainerEngine, container_id: str) -> None:
        self._engine = engine
        self._container_id = container_id

    async def run(
        self, command: str, *, input: str | None = None, timeout: float | None = None
    ) -> RunResult:
        return await self._engine.exec_in(
            self._container_id, command, input=input, timeout=timeout
        )

    async def put_file(self, local: Path, remote: str) -> None:
        source = f"{ARTIFACT_MOUNT}/{local.name}"
        _stdout, stderr, code = await self.run(f"cp {shlex.quote(source)} {shlex.quote(remote)}")
        if code != 0:
            raise BootstrapFailure(f"could not stage {local.name} in the container: {stderr}")


class TransportContainer:
    """Runs qna inside a short-lived (or kept-alive) container."""

    def __init__(
        self,
        image: str,
        *,
        engine: ContainerEngine | None = None,
        arch: str = "x86_64",
        keep_alive: bool = False,
        target: str | None = None,
        state_dir: Path | None = None,
        recheck_prereqs: bool = False,
    ) -> None:
        self.image = image
        self.arch = arch
        self._engine = engine or DockerEngine()
        self._keep_alive = keep_alive
        self._target = target
        self._probed: str | None = None
        self._state_dir = state_dir
        self._recheck_prereqs = recheck_prereqs
        self._container_id: str | None = None

    @property
    def host(self) -> str:
        return f"container:{self.image}@{self.arch}"

    @property
    def platform(self) -> str | None:
        """Docker platform string, so one host can answer for both arches."""
        mapping = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "arm64": "linux/arm64"}
        return mapping.get(self.arch)

    async def aclose(self) -> None:
        if self._container_id is not None:
            await self._engine.stop(self._container_id)
            self._container_id = None

    async def resolve_platform(self, *, timeout_s: float = 30.0) -> str:
        """The :data:`KNOWN_TARGETS` key for this image.

        An explicit ``target`` wins; otherwise the image is probed once and the
        answer cached, the way :class:`TransportSSH` probes a host. The probe
        is classified strictly — the container's answer is authoritative, so a
        guess here would fabricate data (issue #1).
        """
        if self._target is not None:
            return self._target
        if self._probed is None:
            await self._engine.ensure_image(self.image, platform=self.platform)
            if self._container_id is not None:
                stdout, _stderr, _code = await self._engine.exec_in(
                    self._container_id, PLATFORM_PROBE_COMMAND, timeout=timeout_s
                )
            else:
                stdout, _stderr, _code = await self._engine.run_one_shot(
                    self.image, PLATFORM_PROBE_COMMAND, platform=self.platform, timeout=timeout_s
                )
            self._probed = classify_uname(stdout, strict=True)
            logger.debug("probed %s as platform %r", self.image, self._probed)
        return self._probed

    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,
        qna: ResolvedQna | None = None,
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult:
        started = time.monotonic()

        def _result(**overrides: object) -> ClientRelevanceResult:
            base: dict[str, object] = {
                "host": self.host,
                "transport": TRANSPORT_NAME,
                "client_relevance": client_relevance,
                "qna_version": qna.version if qna else None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            base.update(overrides)
            return ClientRelevanceResult(**base)  # type: ignore[arg-type]

        mounts: dict[str, str] = {}
        if qna is not None and qna.artifact_path is not None:
            # Mount the artifact's directory, read-only, so the container can
            # read it but never write back into the controller's cache.
            mounts[str(qna.artifact_path.parent)] = f"{ARTIFACT_MOUNT}:ro"

        transient_container: str | None = None
        try:
            await self._engine.ensure_image(self.image, platform=self.platform)

            # Provisioning needs the real platform (deb vs rpm agent); without
            # provisioning the spec is cosmetic, so the cheap path never probes.
            if qna is not None:
                spec = spec_for(await self.resolve_platform(timeout_s=timeout_s))
            else:
                spec = spec_for(self._target or "ubuntu")

            needs_container = qna is not None or self._keep_alive
            if needs_container:
                container_id = await self._acquire_container(mounts)
                if not self._keep_alive:
                    transient_container = container_id
                runner = _ContainerRunner(self._engine, container_id)

                if qna is not None and self._target is not None:
                    # A probed platform came from the image itself; only a
                    # human-supplied one can contradict what the image carries.
                    await self._check_family(runner, spec)

                if qna is not None:
                    qna_path = await provision_qna(
                        runner,
                        spec,
                        qna,
                        host_label=self.host,
                        state_dir=self._state_dir,
                        recheck_prereqs=self._recheck_prereqs,
                        timeout_s=max(timeout_s, 300.0),
                    )

                stdout, stderr, exit_code = await runner.run(
                    self._eval_command(spec, qna_path),
                    input=normalize_stdin_payload(client_relevance),
                    timeout=timeout_s,
                )
            else:
                stdout, stderr, exit_code = await self._engine.run_one_shot(
                    self.image,
                    self._eval_command(spec, qna_path),
                    input=normalize_stdin_payload(client_relevance),
                    mounts=mounts,
                    platform=self.platform,
                    timeout=timeout_s,
                )
        except UnknownTargetError as exc:
            return _result(error=str(exc), error_kind=ERROR_KIND_BOOTSTRAP)
        except BootstrapFailure as exc:
            return _result(error=str(exc), error_kind=ERROR_KIND_BOOTSTRAP)
        except (ContainerEngineError, OSError, TimeoutError) as exc:
            return _result(error=f"{self.host}: {exc}", error_kind=ERROR_KIND_TRANSPORT)
        except Exception as exc:
            logger.exception("unexpected failure evaluating in %s", self.image)
            return _result(error=f"{self.host}: {exc}", error_kind=ERROR_KIND_TRANSPORT)
        finally:
            if transient_container is not None:
                await self._engine.stop(transient_container)

        parsed = parse_qna_output(stdout)
        error, error_kind = classify_qna_outcome(parsed, exit_code, stderr)

        # A missing binary is a provisioning problem, not a qna crash.
        if error_kind is not None and _looks_like_missing_qna(exit_code, stderr):
            error_kind = ERROR_KIND_BOOTSTRAP
            error = (
                f"no qna in image {self.image}; pass a qna version to provision one "
                f"or use an image with qna baked in ({stderr.strip()})"
            )

        return _result(
            answers=parsed.answers,
            answer_types=parsed.answer_types,
            error=error,
            error_kind=error_kind,
            raw_qna_output=stdout,
            qna_path=qna_path or DEFAULT_QNA_COMMAND,
            qna_time=parsed.qna_time,
            exit_code=exit_code,
        )

    async def _check_family(self, runner: _ContainerRunner, spec: TargetSpec) -> None:
        family = _SPEC_FAMILIES.get(spec.name)
        if family is None:
            return
        stdout, _stderr, _code = await runner.run(FAMILY_SANITY_COMMAND)
        tools = set(stdout.split())
        opposite = {"deb": ("rpm", "dpkg"), "rpm": ("dpkg", "rpm")}[family]
        wrong_tool, right_tool = opposite
        if wrong_tool in tools and right_tool not in tools:
            raise BootstrapFailure(
                f"image {self.image} has {wrong_tool} but not {right_tool}, yet platform "
                f"{spec.name!r} ({family} family) was selected; fix the platform or omit "
                "it to let the image be probed"
            )

    async def _acquire_container(self, mounts: dict[str, str]) -> str:
        if self._keep_alive and self._container_id is not None:
            return self._container_id
        container_id = await self._engine.start(
            self.image, mounts=mounts, platform=self.platform
        )
        if self._keep_alive:
            self._container_id = container_id
        return container_id

    def _eval_command(self, spec: TargetSpec, qna_path: str | None) -> str:
        # API boundary: qna's CLI vocabulary uses "relevance"; internal name
        # stays `client_relevance`.
        binary = qna_path or DEFAULT_QNA_COMMAND
        return " ".join([shlex.quote(binary), *QNA_EVAL_FLAGS])

    def resolved_qna_path(self, version: str) -> str:
        """Where a provisioned version lands inside the container."""
        return qna_path_for(spec_for(self._target or self._probed or "ubuntu"), version)


def _looks_like_missing_qna(exit_code: int, stderr: str) -> bool:
    lowered = stderr.lower()
    return exit_code in (126, 127) or "not found" in lowered or "no such file" in lowered


__all__ = [
    "ARTIFACT_MOUNT",
    "ContainerEngine",
    "ContainerEngineError",
    "DockerEngine",
    "TransportContainer",
    "candidate_docker_sockets",
]
