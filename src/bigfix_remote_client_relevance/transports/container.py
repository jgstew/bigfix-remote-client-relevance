"""Evaluate client relevance inside a container.

Covers the on-demand Linux long tail — "does this answer correctly on Ubuntu
22.04 / RHEL 9 / Amazon Linux 2023?" — with no SSH credentials and no
long-lived VM per distro. It complements rather than replaces SSH, which stays
the answer for macOS and Windows since neither runs meaningfully in a container.

The docker SDK sits behind :class:`ContainerEngine` so another OCI engine
(podman) can slot in, and so the transport is testable without a daemon.

The qna artifact is extracted on the controller (see ``bootstrap.extract_local``)
and the resulting tree bind-mounted read-only, rather than unpacked inside the
target: the engine is local, so a container never needs dpkg-deb/ar+tar or
rpm2cpio+cpio of its own, and a read-only mount means it cannot corrupt the
controller's cache.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, TypeVar, cast

from bigfix_remote_client_relevance.bootstrap.extract_local import (
    LocalExtractionError,
    ensure_extracted,
)
from bigfix_remote_client_relevance.bootstrap.provision import BootstrapFailure, RunResult
from bigfix_remote_client_relevance.bootstrap.targets import (
    TargetSpec,
    UnknownTargetError,
    classify_uname,
    host_arch,
    normalize_arch,
    spec_for,
)
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
    parse_qna_output,
)
from bigfix_remote_client_relevance.transports.container_libs import (
    INDEX_REFRESH_COMMAND,
    PACKAGE_MANAGER_PROBE_COMMAND,
    install_command,
    missing_shared_library,
    needs_index_refresh,
    package_for_soname,
    package_manager_from,
)
from bigfix_remote_client_relevance.transports.container_setup import (
    EngineSetup,
    docker_context_endpoint,
)
from bigfix_remote_client_relevance.transports.coordination import ImageCoordinator
from bigfix_remote_client_relevance.transports.local import (
    QNA_EVAL_FLAGS,
    classify_qna_outcome,
    normalize_stdin_payload,
)

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "container"

T = TypeVar("T")

Extractor = Callable[[ResolvedQna], Awaitable[Path]]

QNA_MOUNT = "/opt/bigfix_qna"
"""Where the controller-extracted qna tree is bind-mounted, read-only."""

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

# Where the extracted tree is staged, read-only, while the prepared image is
# built; distinct from QNA_MOUNT so a build-in-progress container never
# confuses the two.
_BUILD_STAGING_MOUNT = "/opt/.bfrcr_qna_src"

# Installing one library commonly reveals the next, so the build re-probes —
# but a bound keeps a hopeless image from looping.
_MAX_LIBRARY_FIXES = 3

# Building a prepared image copies a tree and may fetch packages over the
# network, which routinely outlasts an evaluation's budget. It is paid once per
# (image, version, arch), so a generous floor costs nothing on the happy path.
_BUILD_TIMEOUT_S = 600.0

# Docker Desktop routinely takes half a minute to accept connections after
# launch; the poll is bounded so a stopped engine cannot hang the run.
_ENGINE_START_TIMEOUT_S = 90.0
_ENGINE_POLL_INTERVAL_S = 2.0


def prepared_image_tag(base_digest: str, version: str, arch: str) -> str:
    """Tag for the image holding ``version`` baked into ``base_digest``.

    Keyed on the base image's own content digest, not its tag name, so a
    moving tag like ``ubuntu:latest`` invalidates the cache on its own.
    """
    short_digest = base_digest.rpartition(":")[2][:12]
    return f"bfrcr/prepared:{short_digest}-{version}-{arch}"


def _platform_parts(platform: str | None) -> tuple[str, str] | None:
    """``"linux/amd64"`` -> ``("linux", "amd64")``, else ``None``."""
    if platform is None or "/" not in platform:
        return None
    os_name, _sep, arch = platform.partition("/")
    return os_name, arch


def _image_platform(image: object) -> tuple[str, str] | None:
    attrs = getattr(image, "attrs", None)
    if not isinstance(attrs, dict):
        return None
    os_name, arch = attrs.get("Os"), attrs.get("Architecture")
    if not os_name or not arch:
        return None
    return str(os_name), str(arch)


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

    async def image_digest(self, image: str) -> str: ...

    async def image_exists(self, image: str) -> bool: ...

    async def commit(self, container_id: str, tag: str) -> None: ...


def candidate_docker_sockets(
    *,
    endpoint_lookup: Callable[[], str | None] = docker_context_endpoint,
    platform: str | None = None,
) -> list[str]:
    """Socket URLs to try, most specific first.

    ``docker.from_env()`` honours ``DOCKER_HOST`` and otherwise assumes
    ``/var/run/docker.sock``. Docker Desktop does create that path, but Docker
    *contexts* are not read by the SDK, so setups that only listen elsewhere —
    Colima, Rancher Desktop, a remote engine, or a Desktop install with the
    compatibility symlink disabled — are unreachable through it. The current
    context is asked first, then each known location is tried, and every path
    tried is reported when none answers.

    ``DOCKER_HOST`` still outranks the context: it is the more explicit
    statement of the two.

    Windows gets the named pipe and nothing else: the Unix-socket fallbacks
    below would render as ``unix://C:\\Users\\...`` there, which names no
    socket that can exist.
    """
    platform = platform if platform is not None else sys.platform

    candidates: list[str] = []
    from_env = os.environ.get("DOCKER_HOST")
    if from_env:
        candidates.append(from_env)
    from_context = endpoint_lookup()
    if from_context:
        candidates.append(from_context)

    if platform.startswith("win"):
        candidates.append("npipe:////./pipe/docker_engine")
        return list(dict.fromkeys(candidates))

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
        self,
        client: object | None = None,
        *,
        socket_candidates: list[str] | None = None,
        auto_setup: bool = True,
        setup: EngineSetup | None = None,
    ) -> None:
        self._client = client
        self._socket_candidates = socket_candidates
        self._auto_setup = auto_setup
        self._setup = setup or EngineSetup()

    def _connect(self, urls: list[str], tried: list[str]) -> object | None:
        """The first URL that answers, or ``None``."""
        import docker

        for url in urls:
            tried.append(url)
            try:
                client = docker.DockerClient(base_url=url)
                client.ping()
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                logger.debug("no container engine at %s: %s", url, exc)
                continue
            logger.debug("connected to the container engine at %s", url)
            return cast(object, client)
        return None

    def _candidates(self) -> list[str]:
        return self._socket_candidates or candidate_docker_sockets()

    def _get_client(self) -> object:
        if self._client is not None:
            return self._client

        tried: list[str] = []
        client = self._connect(self._candidates(), tried)

        if client is None and self._auto_setup:
            client = self._start_engine_and_wait(tried)

        if client is not None:
            self._client = client
            return client

        raise ContainerEngineError(
            f"cannot connect to the Docker daemon; {self._setup.hint()}. tried: "
            + ", ".join(tried)
        )

    def _start_engine_and_wait(self, tried: list[str]) -> object | None:
        """Start an installed-but-stopped engine, then wait for it to answer."""
        starter = self._setup.detect()
        if starter is None:
            return None
        if starter.argv is None:
            # Detected, but starting it needs privileges that are not ours to
            # take — say what to run instead.
            raise ContainerEngineError(
                f"{starter.name} is installed but not running; {starter.note}. tried: "
                + ", ".join(tried)
            )

        logger.info("no container engine answered; starting %s", starter.name)
        self._setup.start(starter)

        waited = 0.0
        while waited < _ENGINE_START_TIMEOUT_S:
            self._setup.sleep(_ENGINE_POLL_INTERVAL_S)
            waited += _ENGINE_POLL_INTERVAL_S
            # Re-derived every pass: the socket path, and the docker context
            # naming it, often only appear once the engine is up.
            client = self._connect(self._candidates(), [])
            if client is not None:
                logger.info("%s is ready after %.0fs", starter.name, waited)
                return client
            if waited % 10 < _ENGINE_POLL_INTERVAL_S:
                logger.info(
                    "waiting for %s (%.0fs/%.0fs)...",
                    starter.name,
                    waited,
                    _ENGINE_START_TIMEOUT_S,
                )

        raise ContainerEngineError(
            f"{starter.name} did not become ready within "
            f"{_ENGINE_START_TIMEOUT_S:.0f}s; start it manually and re-run. tried: "
            + ", ".join(tried)
        )

    async def ensure_image(self, image: str, *, platform: str | None = None) -> None:
        def _ensure() -> None:
            import docker.errors

            client = self._get_client()
            try:
                found = client.images.get(image)  # type: ignore[attr-defined]
            except docker.errors.ImageNotFound:
                logger.info("pulling image %s", image)
                client.images.pull(image, platform=platform)  # type: ignore[attr-defined]
                return

            # images.get ignores platform: a cached image of the wrong
            # architecture satisfies it, then container creation 404s. Compare
            # what's actually on disk and re-pull on mismatch.
            wanted = _platform_parts(platform)
            if wanted is not None and _image_platform(found) not in (None, wanted):
                logger.info(
                    "cached %s is %s, not %s; re-pulling", image, _image_platform(found), wanted
                )
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

    async def image_digest(self, image: str) -> str:
        def _digest() -> str:
            client = self._get_client()
            return str(client.images.get(image).id)  # type: ignore[attr-defined]

        return await self._guard(_digest)

    async def image_exists(self, image: str) -> bool:
        def _exists() -> bool:
            import docker.errors

            client = self._get_client()
            try:
                client.images.get(image)  # type: ignore[attr-defined]
            except docker.errors.ImageNotFound:
                return False
            return True

        return await self._guard(_exists)

    async def commit(self, container_id: str, tag: str) -> None:
        def _commit() -> None:
            client = self._get_client()
            container = client.containers.get(container_id)  # type: ignore[attr-defined]
            repository, _sep, tag_name = tag.partition(":")
            container.commit(repository=repository, tag=tag_name or "latest")

        await self._guard(_commit)

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
        extractor: Extractor | None = None,
        rebuild_image: bool = False,
        auto_setup: bool = True,
        coordinator: ImageCoordinator | None = None,
    ) -> None:
        self.image = image
        self.arch = arch
        self._engine = engine or DockerEngine(auto_setup=auto_setup)
        self._keep_alive = keep_alive
        self._target = target
        self._probed: str | None = None
        self._extractor = extractor or ensure_extracted
        self._rebuild_image = rebuild_image
        self._auto_setup = auto_setup
        self._container_id: str | None = None
        self._warned_about_emulation = False
        # A private coordinator still works — it just shares nothing, which is
        # right for a transport used on its own rather than through a fan-out.
        self._coordinator = coordinator or ImageCoordinator()
        self._prepared: tuple[str, tuple[str, dict[str, str]]] | None = None

    @property
    def host(self) -> str:
        return f"container:{self.image}@{self.arch}"

    @property
    def platform(self) -> str | None:
        """Docker platform string, so one host can answer for both arches."""
        mapping = {"x86_64": "linux/amd64", "arm64": "linux/arm64"}
        return mapping.get(normalize_arch(self.arch))

    def _note_emulation(self) -> None:
        """Say when the container's architecture is not this machine's.

        BigFix publishes no arm64 agent for any platform this tool targets, so
        on Apple Silicon this is every run — emulated, slower, and occasionally
        behaving differently from native. Worth knowing about; not worth
        repeating per evaluation.
        """
        if self._warned_about_emulation:
            return
        self._warned_about_emulation = True
        target_arch = normalize_arch(self.arch)
        if target_arch != host_arch():
            logger.info(
                "running %s as %s while this machine is %s: emulated, so slower than "
                "native and occasionally different. BigFix publishes no %s agent, so "
                "this is the only option.",
                self.image,
                target_arch,
                host_arch(),
                host_arch(),
            )

    async def aclose(self) -> None:
        if self._container_id is not None:
            await self._engine.stop(self._container_id)
            self._container_id = None

    async def _ensure_image_once(self) -> None:
        """Pull the base image, at most once per image across a whole run."""
        await self._coordinator.once(
            f"pull:{self.image}@{self.platform}",
            lambda: self._engine.ensure_image(self.image, platform=self.platform),
        )

    async def prepare(self, *, qna: ResolvedQna | None = None, timeout_s: float = 30.0) -> None:
        """Do the image work up front, so a fan-out can budget it separately.

        Purely an optimization: :meth:`evaluate_client_relevance` does the same
        work itself when this was not called, or was called for a different
        version. That keeps the transport correct when used on its own, which
        is how it is normally driven outside the orchestrator.
        """
        self._note_emulation()
        await self._ensure_image_once()
        if qna is None:
            return
        spec = spec_for(await self.resolve_platform(timeout_s=timeout_s))
        self._prepared = (qna.version, await self._prepare_qna_image(qna, spec, timeout_s=timeout_s))

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
            await self._ensure_image_once()
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
        image_to_run = self.image

        transient_container: str | None = None
        try:
            self._note_emulation()
            await self._ensure_image_once()

            # Provisioning needs the real platform (deb vs rpm agent); without
            # provisioning the spec is cosmetic, so the cheap path never probes.
            if qna is not None:
                spec = spec_for(await self.resolve_platform(timeout_s=timeout_s))
            else:
                spec = spec_for(self._target or "ubuntu")

            if qna is not None:
                if self._prepared is not None and self._prepared[0] == qna.version:
                    image_to_run, mounts = self._prepared[1]
                    mounts = dict(mounts)
                else:
                    image_to_run, mounts = await self._prepare_qna_image(
                        qna, spec, timeout_s=timeout_s
                    )
                qna_path = posixpath.join(QNA_MOUNT, spec.qna_relative_path)

            # A probed platform came from the image itself; only a
            # human-supplied one can contradict what the image carries, so
            # only that case pays for the sanity check.
            needs_sanity = (
                qna is not None and self._target is not None and spec.name in _SPEC_FAMILIES
            )
            needs_container = self._keep_alive or needs_sanity

            if needs_container:
                container_id = await self._acquire_container(mounts, image=image_to_run)
                if not self._keep_alive:
                    transient_container = container_id
                if needs_sanity:
                    await self._check_family(container_id, spec)
                stdout, stderr, exit_code = await self._engine.exec_in(
                    container_id,
                    self._eval_command(spec, qna_path),
                    input=normalize_stdin_payload(client_relevance),
                    timeout=timeout_s,
                )
            else:
                stdout, stderr, exit_code = await self._engine.run_one_shot(
                    image_to_run,
                    self._eval_command(spec, qna_path),
                    input=normalize_stdin_payload(client_relevance),
                    mounts=mounts,
                    platform=self.platform,
                    timeout=timeout_s,
                )
        except UnknownTargetError as exc:
            return _result(error=str(exc), error_kind=ERROR_KIND_BOOTSTRAP)
        except (BootstrapFailure, LocalExtractionError) as exc:
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

        # qna is there but the image lacks something it links against.
        soname = missing_shared_library(stderr)
        if error_kind is not None and soname is not None:
            error_kind = ERROR_KIND_BOOTSTRAP
            error = (
                f"qna cannot start in image {self.image}: missing shared library "
                f"{soname}; install the package providing it in the image "
                f"({stderr.strip()})"
            )

        # A missing binary is a provisioning problem, not a qna crash.
        elif error_kind is not None and _looks_like_missing_qna(exit_code, stderr):
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

    async def _check_family(self, container_id: str, spec: TargetSpec) -> None:
        family = _SPEC_FAMILIES[spec.name]
        stdout, _stderr, _code = await self._engine.exec_in(container_id, FAMILY_SANITY_COMMAND)
        tools = set(stdout.split())
        opposite = {"deb": ("rpm", "dpkg"), "rpm": ("dpkg", "rpm")}[family]
        wrong_tool, right_tool = opposite
        if wrong_tool in tools and right_tool not in tools:
            raise BootstrapFailure(
                f"image {self.image} has {wrong_tool} but not {right_tool}, yet platform "
                f"{spec.name!r} ({family} family) was selected; fix the platform or omit "
                "it to let the image be probed"
            )

    async def _acquire_container(self, mounts: dict[str, str], *, image: str | None = None) -> str:
        if self._keep_alive and self._container_id is not None:
            return self._container_id
        container_id = await self._engine.start(
            image or self.image, mounts=mounts, platform=self.platform
        )
        if self._keep_alive:
            self._container_id = container_id
        return container_id

    async def _prepare_qna_image(
        self, qna: ResolvedQna, spec: TargetSpec, *, timeout_s: float
    ) -> tuple[str, dict[str, str]]:
        """Return ``(image, mounts)`` to run the eval against.

        The first run against a given (base image digest, version, arch)
        builds a derived image with the qna tree baked in and commits it;
        later runs start that image directly, no mount and no extraction.
        A base image without a shell to build in (e.g. distroless) falls
        back to mounting the extracted tree, same as before this cache
        existed.
        """
        key = f"prepared:{self.image}@{self.arch}:{qna.version}:{self._rebuild_image}"

        async def build() -> tuple[str, dict[str, str]]:
            digest = await self._engine.image_digest(self.image)
            tag = prepared_image_tag(digest, qna.version, self.arch)
            if self._rebuild_image or not await self._engine.image_exists(tag):
                # Only pay for extraction when a build (or its fallback) needs
                # the tree; a cache hit skips both.
                tree = await self._extractor(qna)
                if await self._build_prepared_image(tree, tag, spec, timeout_s=timeout_s):
                    return tag, {}
                return self.image, {str(tree): f"{QNA_MOUNT}:ro"}
            return tag, {}

        image, mounts = await self._coordinator.once(key, build)
        # The mounts dict is shared between everyone waiting on this key.
        return image, dict(mounts)

    async def _build_prepared_image(
        self, tree: Path, tag: str, spec: TargetSpec, *, timeout_s: float
    ) -> bool:
        timeout_s = max(timeout_s, _BUILD_TIMEOUT_S)
        container_id = await self._engine.start(
            self.image,
            mounts={str(tree): f"{_BUILD_STAGING_MOUNT}:ro"},
            platform=self.platform,
        )
        try:
            _stdout, stderr, exit_code = await self._engine.exec_in(
                container_id,
                f"cp -a {_BUILD_STAGING_MOUNT} {QNA_MOUNT} && chmod -R a+rX {QNA_MOUNT}",
                timeout=timeout_s,
            )
            if exit_code != 0:
                logger.info(
                    "could not build a prepared image for %s, mounting instead: %s",
                    self.image,
                    stderr.strip(),
                )
                return False

            if not await self._make_qna_runnable(container_id, spec, timeout_s=timeout_s):
                return False

            await self._engine.commit(container_id, tag)
            return True
        finally:
            await self._engine.stop(container_id)

    async def _make_qna_runnable(
        self, container_id: str, spec: TargetSpec, *, timeout_s: float
    ) -> bool:
        """Install whatever the image is missing, until qna links or we give up.

        Returns whether the image is worth committing. Anything short of a
        clean link returns False, which drops the caller back to mounting the
        extracted tree: the evaluation then fails loudly naming the library,
        and nothing broken is cached, so a later run with network access (or
        without ``--no-auto-setup``) retries from scratch.
        """
        qna_path = posixpath.join(QNA_MOUNT, spec.qna_relative_path)
        probe = f"{shlex.quote(qna_path)} {' '.join(QNA_EVAL_FLAGS)} < /dev/null"
        family = _SPEC_FAMILIES.get(spec.name)
        manager: str | None = None
        refreshed = False
        attempted: set[str] = set()

        for _attempt in range(_MAX_LIBRARY_FIXES):
            _stdout, stderr, _code = await self._engine.exec_in(
                container_id, probe, timeout=timeout_s
            )
            soname = missing_shared_library(stderr)
            if soname is None:
                # Either it links, or it failed for a reason no install fixes;
                # an image whose probe cannot run at all lands here too and is
                # committed exactly as it was before this check existed.
                return True
            if not self._auto_setup:
                logger.info(
                    "%s is missing %s and auto-setup is off; not installing it",
                    self.image,
                    soname,
                )
                return False
            if soname in attempted:
                logger.warning(
                    "installing for %s did not make qna runnable in %s", soname, self.image
                )
                return False
            attempted.add(soname)

            if manager is None:
                stdout, _stderr, _code = await self._engine.exec_in(
                    container_id, PACKAGE_MANAGER_PROBE_COMMAND, timeout=timeout_s
                )
                manager = package_manager_from(stdout)
            if manager is None:
                logger.warning(
                    "%s needs %s but has no package manager to install it with",
                    self.image,
                    soname,
                )
                return False

            package = package_for_soname(soname, family=family, manager=manager)
            if package is None:
                logger.warning(
                    "no known package provides %s on the %s family; add a mapping or "
                    "install it in the image yourself",
                    soname,
                    family,
                )
                return False

            if needs_index_refresh(manager) and not refreshed:
                await self._engine.exec_in(
                    container_id, INDEX_REFRESH_COMMAND, timeout=timeout_s
                )
                refreshed = True

            logger.info("installing %s in %s to provide %s", package, self.image, soname)
            _stdout, stderr, exit_code = await self._engine.exec_in(
                container_id, install_command(manager, package), timeout=timeout_s
            )
            if exit_code != 0:
                logger.warning(
                    "could not install %s in %s: %s", package, self.image, stderr.strip()
                )
                return False

        logger.warning(
            "gave up making qna runnable in %s after %d attempts", self.image, _MAX_LIBRARY_FIXES
        )
        return False

    def _eval_command(self, spec: TargetSpec, qna_path: str | None) -> str:
        # API boundary: qna's CLI vocabulary uses "relevance"; internal name
        # stays `client_relevance`.
        binary = qna_path or DEFAULT_QNA_COMMAND
        return " ".join([shlex.quote(binary), *QNA_EVAL_FLAGS])

    def resolved_qna_path(self, version: str) -> str:
        """Where a provisioned version lands inside the container."""
        spec = spec_for(self._target or self._probed or "ubuntu")
        return posixpath.join(QNA_MOUNT, spec.qna_relative_path)


def _looks_like_missing_qna(exit_code: int, stderr: str) -> bool:
    # A binary that cannot link exits 127 too, and the linker's message ends in
    # "No such file or directory" — so that case is ruled out first, or every
    # missing shared library would be reported as a missing binary.
    if missing_shared_library(stderr) is not None:
        return False
    lowered = stderr.lower()
    return exit_code in (126, 127) or "not found" in lowered or "no such file" in lowered


__all__ = [
    "QNA_MOUNT",
    "ContainerEngine",
    "ContainerEngineError",
    "DockerEngine",
    "TransportContainer",
    "candidate_docker_sockets",
]
