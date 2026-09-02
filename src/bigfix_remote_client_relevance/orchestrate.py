"""Fan out one client-relevance expression across targets and qna versions.

This is the single entry point the CLI sits on and the future MCP tool will
call. It owns three things transports deliberately do not:

* **Version-spec resolution.** A spec like ``"11.0"`` is resolved once per run
  and the resulting full version flows everywhere downstream, so results always
  record what was actually evaluated rather than what was asked for.
* **The (targets x versions) grid**, producing one result per pair.
* **Concurrency**, bounded by ``max_parallel`` — a 10-host, 2-version run is 20
  units of work, not 20 simultaneous connections.

Failures never propagate as exceptions: one unreachable host yields one result
with ``error_kind`` set while every other pair still evaluates.

Two entry points over the same machinery: ``evaluate_client_relevance``
returns the whole list in target-then-version order, and
``evaluate_client_relevance_stream`` yields each result the moment its pair
finishes, so a slow host does not hold up the ones that already answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError
from bigfix_remote_client_relevance.bootstrap.targets import UnknownTargetError
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_RESOLVE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports import Transport

logger = logging.getLogger(__name__)

# Exit codes, actionable for CI gating. The worst across the fan-out wins.
EXIT_OK = 0
EXIT_RELEVANCE = 1
EXIT_QNA = 2
EXIT_TRANSPORT = 3
EXIT_RESOLVE = 4

_EXIT_BY_KIND: dict[str | None, int] = {
    None: EXIT_OK,
    ERROR_KIND_RELEVANCE: EXIT_RELEVANCE,
    ERROR_KIND_QNA: EXIT_QNA,
    ERROR_KIND_BOOTSTRAP: EXIT_QNA,
    ERROR_KIND_TRANSPORT: EXIT_TRANSPORT,
    ERROR_KIND_RESOLVE: EXIT_RESOLVE,
}

DEFAULT_MAX_PARALLEL = 8

# Pulling images and evaluating relevance cost wildly different things: eight
# simultaneous multi-hundred-MB pulls will saturate a laptop's link while eight
# evaluations barely register. So they get separate budgets.
DEFAULT_PULL_PARALLEL = 2


@dataclass
class Target:
    """One place to evaluate: an SSH host, a container image, or this machine."""

    kind: str
    """``"ssh"`` | ``"local"`` | ``"container"`` | ``"fastquery"`` |
    ``"online_evaluator"``."""

    name: str
    """SSH alias, image name, or ``"local"``."""

    user: str | None = None
    become: bool | None = None
    """None means unspecified: `default_transport_factory` decides -- True for
    `local` on a macOS controller (qna needs root there unconditionally),
    False otherwise. An explicit True/False always wins."""
    image: str | None = None
    arch: str | None = None
    """``"x86_64"``, ``"arm64"``, etc. None means unset: probed for ssh/local
    targets (see ``_one()``'s arch-probe block), falling back to ``"x86_64"``
    if the probe fails -- the common case for BigFix clients. Container
    transports always get an explicit value -- the CLI defaults ``--arch`` to
    ``"x86_64"`` too, regardless of the controller's own architecture."""
    engine: str = "auto"
    """Container only. ``"auto"`` | ``"docker"`` | ``"podman"``. ``"auto"``
    prefers docker, falling back to podman only when docker is unreachable --
    a no-op for anyone not using podman."""
    platform: str | None = None
    """Bootstrap target key (``"ubuntu"``, ``"macos"``, ...); probed if None."""

    qna_version: str | Sequence[str] | None = None
    """Per-target override of the run-wide version spec."""

    keep_alive: bool = False
    idle_ttl_s: float | None = None
    """Container only. How long a container survives its last use before
    removing itself; None takes the transport's own default. A kept-alive
    container is reclaimed by this and nothing else, since it deliberately
    outlives the process that started it."""

    rebuild_image: bool = False
    """Container only. Force a fresh prepared image instead of reusing a cached one."""

    auto_setup: bool = True
    """Container only. Install runtime libraries the image is missing. Off for air-gapped hosts."""

    verify_host_key: bool = True
    """SSH only. Off removes protection against interception; lab hosts only."""

    base_url: str | None = None
    """``online_evaluator`` only. Origin of the hosted relevance API, e.g.
    ``"https://developer.bigfix.com"``. Required for that kind -- there is no
    default, since pointing this at a live third-party service is an opt-in
    choice (see :class:`~...transports.online_evaluator.TransportOnlineEvaluator`)."""

    extra: dict[str, object] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.kind == "container":
            # `arch or "x86_64"` mirrors what default_transport_factory
            # actually runs with: a container target left unset (an inventory
            # host with no `arch` line) is never probed, it falls back. The
            # label is the `host` every failure is reported under, so it has
            # to name the arch that was really used, not a bare "None".
            return f"container:{self.image or self.name}@{self.arch or 'x86_64'}"
        return self.name


TransportFactory = Callable[[Target], Transport]
Resolver = Callable[[str | None, Target], Coroutine[Any, Any, ResolvedQna]]


def default_transport_factory(target: Target, *, coordinator: object | None = None) -> Transport:
    """Build the transport a target calls for.

    ``coordinator`` is how a fan-out shares image work between the transports
    it builds; it is keyword-only and optional so the one-argument
    :data:`TransportFactory` signature still holds.
    """
    if target.kind == "local":
        from bigfix_remote_client_relevance.transports.local import TransportLocal

        # qna requires root on macOS unconditionally, so an unspecified
        # `become` defaults on there; SSH can't make the same call below --
        # the remote platform isn't known without a round trip.
        become = target.become if target.become is not None else sys.platform == "darwin"
        return TransportLocal(
            target=target.platform, become=become, host=target.name, arch=target.arch
        )
    if target.kind == "ssh":
        from bigfix_remote_client_relevance.transports.ssh import TransportSSH

        return TransportSSH(
            target.name,
            user=target.user,
            become=bool(target.become),
            target=target.platform,
            verify_host_key=target.verify_host_key,
            arch=target.arch,
        )
    if target.kind == "container":
        from bigfix_remote_client_relevance.transports.container import (
            DEFAULT_IDLE_TTL_S,
            ContainerEngine,
            ContainerEngineError,
            DockerEngine,
            PodmanEngine,
            TransportContainer,
        )

        engine: ContainerEngine | None = None
        if target.engine == "podman":
            engine = PodmanEngine(auto_setup=target.auto_setup)
        elif target.engine == "docker":
            engine = DockerEngine(auto_setup=target.auto_setup)
        elif target.engine == "auto":
            # docker-preferred, matching detect_engine_starter's own ordering,
            # so "auto" changes nothing for anyone not using podman.
            docker_engine = DockerEngine(auto_setup=target.auto_setup)
            try:
                docker_engine._get_client()
                engine = docker_engine
            except ContainerEngineError:
                engine = PodmanEngine(auto_setup=target.auto_setup)
        else:
            raise ValueError(f"unknown engine {target.engine!r}")

        return TransportContainer(
            target.image or target.name,
            # Defensive fallback for a programmatic Target(kind="container")
            # built without arch; CLI-built container targets are never None
            # (--arch itself defaults to "x86_64", the common case for BigFix
            # clients, regardless of the controller's own architecture).
            arch=target.arch or "x86_64",
            engine=engine,
            keep_alive=target.keep_alive,
            # None here means "unset", not "no deadline" -- an inventory host
            # that says nothing about its idle window gets the default one,
            # the same as a container built without the argument at all.
            idle_ttl_s=DEFAULT_IDLE_TTL_S if target.idle_ttl_s is None else target.idle_ttl_s,
            target=target.platform,
            rebuild_image=target.rebuild_image,
            auto_setup=target.auto_setup,
            coordinator=coordinator,  # type: ignore[arg-type]
        )
    if target.kind == "fastquery":
        from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery

        return TransportFastQuery(target.extra.get("besapi_client"))
    if target.kind == "online_evaluator":
        from bigfix_remote_client_relevance.transports.online_evaluator import (
            TransportOnlineEvaluator,
        )

        if not target.base_url:
            raise ValueError("online_evaluator target needs base_url")
        return TransportOnlineEvaluator(target.base_url, host=target.name)
    raise ValueError(f"unknown target kind {target.kind!r}")


async def default_resolver(spec: str | None, target: Target) -> ResolvedQna:
    """Resolve a version spec and make sure its artifact is cached locally."""
    from bigfix_remote_client_relevance.bootstrap.cache import ensure_artifact
    from bigfix_remote_client_relevance.bootstrap.release_site import (
        artifact_for,
        resolve_version_spec,
    )
    from bigfix_remote_client_relevance.bootstrap.targets import spec_for

    # Containers and SSH both now expose resolve_platform, so _one() probes
    # either before it ever reaches here -- target.platform is never None for
    # them in the normal fan-out. "ubuntu" is a defensive fallback for
    # `local` (no comparable probe exists) and for a resolver called
    # directly, bypassing _one()'s probe step.
    platform_key = target.platform or ("macos" if target.kind == "local" else "ubuntu")
    release_platform = spec_for(platform_key).release_platform

    # Likewise, _one() probes arch for ssh/local before ever reaching here --
    # target.arch is never None in the normal fan-out. "x86_64" is a
    # defensive fallback for a resolver called directly, bypassing that step.
    version = await asyncio.to_thread(resolve_version_spec, spec)
    ref = await asyncio.to_thread(
        artifact_for, version, platform=release_platform, arch=target.arch or "x86_64"
    )
    return await ensure_artifact(version, ref)


def _version_specs(target: Target, run_wide: str | Sequence[str] | None) -> list[str | None]:
    """Which version specs apply to this target, per-target override winning."""
    chosen = target.qna_version if target.qna_version is not None else run_wide
    if chosen is None:
        return [None]
    if isinstance(chosen, str):
        return [chosen]
    return list(chosen) or [None]


def count_work(
    targets: Sequence[Target],
    qna_version: str | Sequence[str] | None = None,
    expressions: Sequence[str] | int | None = None,
) -> int:
    """How many results a fan-out over these arguments will produce.

    Exposed because a streaming caller has to decide how to format its first
    result before it knows how many follow, and this is not simply
    ``len(targets)``: per-host ``qna_version`` overrides mean each target can
    contribute a different number of pairs, and a batch multiplies each pair by
    its expressions.
    """
    if expressions is None:
        per_pair = 1
    elif isinstance(expressions, int):
        per_pair = expressions
    else:
        per_pair = len(expressions)
    return sum(len(_version_specs(target, qna_version)) for target in targets) * per_pair


async def _release(transport: object) -> None:
    """Hand a transport back once its work is done.

    Optional by design: ``local`` and ``fastquery`` hold nothing that needs
    releasing and expose no ``aclose``. A failure here is logged, never
    raised -- a run that produced its answers must not fail on cleanup.
    """
    closer = getattr(transport, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 - cleanup never fails a run
        logger.debug("releasing a transport failed: %s", exc)


async def _evaluate_stream_indexed(
    expressions: Sequence[str],
    targets: Sequence[Target],
    *,
    qna_version: str | Sequence[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    pull_parallel: int = DEFAULT_PULL_PARALLEL,
    timeout_s: float = 30.0,
    transport_factory: TransportFactory | None = None,
    resolver: Resolver | None = None,
) -> AsyncIterator[tuple[int, ClientRelevanceResult]]:
    """Shared core: yield ``(work_index, result)`` as each cell finishes.

    The unit of *work* is a **group** -- one (target, version) pair -- and the
    unit of *result* is a cell, one expression within a group. A group builds
    its transport, probes, and prepares once, then runs every expression
    through that one transport in sequence: that is what makes a batch cheaper
    than the same expressions run separately, and for a container it is the
    difference between one `docker run` and N.

    ``work_index`` is the cell's position in target-then-version-then-expression
    order, kept so callers that need that ordering
    (:func:`evaluate_client_relevance`) can reconstruct it even though
    completion order is whatever finishes first. Every failure mode is reported
    inside a result rather than raised.
    """
    if transport_factory is None:
        # One coordinator per run, so the transports built below share image
        # work with each other and with nothing else.
        from bigfix_remote_client_relevance.transports.coordination import ImageCoordinator

        coordinator = ImageCoordinator()

        def _default_transport_factory(target: Target) -> Transport:
            return default_transport_factory(target, coordinator=coordinator)

        transport_factory = _default_transport_factory

    resolver = resolver or default_resolver
    semaphore = asyncio.Semaphore(max_parallel)
    image_semaphore = asyncio.Semaphore(pull_parallel)

    # Resolve each distinct spec once per run and share the outcome, so a
    # 10-host run does not scrape the release site 10 times.
    resolutions: dict[tuple[str | None, str], asyncio.Task[ResolvedQna]] = {}
    work: list[tuple[Target, str | None]] = [
        (target, spec) for target in targets for spec in _version_specs(target, qna_version)
    ]

    def _failure(
        target: Target, kind: str, message: str, client_relevance: str
    ) -> ClientRelevanceResult:
        return ClientRelevanceResult(
            host=target.label,
            transport=target.kind,
            client_relevance=client_relevance,
            error=message,
            error_kind=kind,
        )

    def _group_failure(target: Target, kind: str, message: str) -> list[ClientRelevanceResult]:
        """The same failure, once per expression.

        A group that never gets as far as evaluating still owes one result per
        cell: ``count_work`` is the denominator a progress indicator commits to
        before the first result arrives, and a short run would silently
        misreport rather than fail.
        """
        return [_failure(target, kind, message, expression) for expression in expressions]

    async def _resolve(target: Target, spec: str | None) -> ResolvedQna:
        # One task per (spec, arch): targets sharing both share the download.
        key = (spec, f"{target.platform or target.kind}-{target.arch}")
        if key not in resolutions:
            resolutions[key] = asyncio.create_task(resolver(spec, target))
        return await resolutions[key]

    async def _group(
        target: Target, spec: str | None, emit: Callable[[int, ClientRelevanceResult], Any]
    ) -> None:
        """Evaluate every expression against one (target, version) pair."""
        resolved: ResolvedQna | None = None
        # Captured before any probe-before-resolve replaces target.platform,
        # so the corrective reprobe below only fires for a platform the
        # caller actually set (never for one this run just probed itself).
        configured_platform = target.platform

        if len(expressions) > 1 and target.kind == "container" and not target.keep_alive:
            # Without this the second expression starts a second container and
            # the batch saves nothing. A single expression is left one-shot:
            # there is nothing to amortize, and one-shot is the more hermetic
            # default.
            target = dataclasses.replace(target, keep_alive=True)

        try:
            transport = transport_factory(target)
        except Exception as exc:  # noqa: BLE001 - one bad target never fails the run
            logger.debug("transport construction failed for %s: %s", target.label, exc)
            for index, result in enumerate(
                _group_failure(target, ERROR_KIND_TRANSPORT, f"{target.label}: {exc}")
            ):
                emit(index, result)
            return

        try:
            prepare = getattr(transport, "prepare", None)
            # Only image-backed transports have an image phase to budget; an SSH
            # sweep must not be serialized by a container-pull limit.
            image_budget: AbstractAsyncContextManager[Any] = (
                image_semaphore if prepare is not None else contextlib.nullcontext()
            )

            if spec is not None and target.kind == "fastquery":
                # Endpoints evaluate with their installed agent; refuse before
                # doing any work rather than resolving a version nothing can use.
                for index, result in enumerate(
                    _group_failure(
                        target,
                        ERROR_KIND_RESOLVE,
                        f"cannot pin qna version {spec!r} for the fastquery transport: "
                        "endpoints evaluate with their installed BES agent",
                    )
                ):
                    emit(index, result)
                return

            if spec is not None and target.kind == "online_evaluator":
                # Same reasoning as fastquery above: this transport's environment
                # is fixed and remotely managed, so resolving a version here would
                # just spend a round trip on a spec nothing downstream can use.
                for index, result in enumerate(
                    _group_failure(
                        target,
                        ERROR_KIND_RESOLVE,
                        f"cannot pin qna version {spec!r} for the online_evaluator "
                        "transport: it evaluates on a fixed, remotely-managed "
                        "environment",
                    )
                ):
                    emit(index, result)
                return

            # The resolver picks the artifact (deb vs rpm) from the platform, so an
            # unset one must be probed BEFORE resolution -- gated on spec being set,
            # since for ssh/container this is a real round trip not worth paying for
            # a host that's never going to resolve a version anyway (e.g.
            # `qna_version = []`, probing whatever's installed). Local's probe is
            # the exception: it's just a `sys.platform` check, no round trip, so it
            # runs unconditionally -- otherwise an unpinned local entry would never
            # get its platform written back (`--update-inventory`) or shown in its
            # header (`render.label`), the entire reason `resolve_platform` was
            # added to it in the first place. For a container the probe can trigger
            # the image pull, so it shares the image budget.
            probe = getattr(transport, "resolve_platform", None)
            if (
                target.platform is None
                and probe is not None
                and (spec is not None or target.kind == "local")
            ):
                try:
                    async with image_budget:
                        probed = await probe(timeout_s=timeout_s)
                    target = dataclasses.replace(target, platform=probed)
                except UnknownTargetError as exc:
                    for index, result in enumerate(
                        _group_failure(target, ERROR_KIND_BOOTSTRAP, str(exc))
                    ):
                        emit(index, result)
                    return
                except Exception as exc:  # noqa: BLE001 - a bad probe never kills a run
                    logger.debug("platform probe failed for %s: %s", target.label, exc)
                    for index, result in enumerate(
                        _group_failure(target, ERROR_KIND_TRANSPORT, f"{target.label}: {exc}")
                    ):
                        emit(index, result)
                    return

            # Same probe-before-resolve idea, for arch: ssh/local targets expose
            # resolve_arch (container does not -- its arch is always an explicit,
            # intentional per-run choice, never an unknown to infer). Unlike
            # platform, a failed arch probe never fails the target -- "x86_64",
            # the common case for BigFix clients, is always a reasonable
            # fallback, so there is no analog to UnknownTargetError here.
            arch_probe = getattr(transport, "resolve_arch", None)
            if (
                target.arch is None
                and arch_probe is not None
                and (spec is not None or target.kind == "local")
            ):
                try:
                    async with image_budget:
                        probed_arch = await arch_probe(timeout_s=timeout_s)
                except Exception as exc:  # noqa: BLE001 - arch always has a safe fallback
                    logger.debug("arch probe failed for %s: %s", target.label, exc)
                    probed_arch = "x86_64"
                target = dataclasses.replace(target, arch=probed_arch)

            if spec is not None:
                try:
                    resolved = await _resolve(target, spec)
                except ResolveError as exc:
                    for index, result in enumerate(
                        _group_failure(target, ERROR_KIND_RESOLVE, str(exc))
                    ):
                        emit(index, result)
                    return
                except Exception as exc:  # noqa: BLE001 - resolution never kills a run
                    logger.debug("resolution failed for %s: %s", spec, exc)
                    for index, result in enumerate(
                        _group_failure(
                            target, ERROR_KIND_RESOLVE, f"could not resolve {spec!r}: {exc}"
                        )
                    ):
                        emit(index, result)
                    return

            if prepare is not None:
                # Pulling and building under their own budget. Failures are not
                # fatal: this is an optimization, and the evaluation below hits the
                # same code path and reports the failure in its own vocabulary.
                try:
                    async with image_budget:
                        await prepare(qna=resolved, timeout_s=timeout_s)
                except Exception as exc:  # noqa: BLE001 - the evaluation will report it
                    logger.debug("image preparation failed for %s: %s", target.label, exc)

            for index, expression in enumerate(expressions):
                emit(
                    index,
                    await _evaluate_one(
                        transport,
                        target,
                        expression,
                        resolved=resolved,
                        spec=spec,
                        configured_platform=configured_platform,
                    ),
                )
        finally:
            # Nothing else in the process holds this transport, so an
            # unreleased one leaks whatever it opened -- an SSH connection,
            # or (worse, because it outlives the process) a kept-alive
            # container.
            await _release(transport)

    async def _evaluate_one(
        transport: Transport,
        target: Target,
        client_relevance: str,
        *,
        resolved: ResolvedQna | None,
        spec: str | None,
        configured_platform: str | None,
    ) -> ClientRelevanceResult:
        """One expression, against an already-prepared transport.

        The semaphore is held around the evaluation itself rather than the
        whole group, so a ten-expression batch does not occupy a concurrency
        slot for the length of its serial run.
        """
        started = time.monotonic()
        async with semaphore:
            try:
                result = await transport.evaluate_client_relevance(
                    client_relevance, qna=resolved, timeout_s=timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - one bad cell never fails the run
                logger.debug("transport failed for %s: %s", target.label, exc)
                return _failure(
                    target, ERROR_KIND_TRANSPORT, f"{target.label}: {exc}", client_relevance
                )

        # Whatever platform/arch this run actually used -- explicit, freshly
        # probed, or (platform only, below) corrected -- so the CLI can write
        # them back.
        result.platform = target.platform
        result.arch = target.arch

        if spec is not None and configured_platform is not None:
            # An explicit platform was trusted outright above and never
            # probed. If it made the run fail, that's exactly the failure
            # mode a wrong remote_clients.toml entry produces (issue: a stale or
            # mistyped platform silently resolves the wrong artifact) --
            # cheap to double-check now that it's failed, on the same live
            # connection, rather than leaving it to fail identically forever.
            reprobe = getattr(transport, "reprobe_platform", None)
            if reprobe is not None and result.error_kind == ERROR_KIND_BOOTSTRAP:
                try:
                    actual = await reprobe(timeout_s=timeout_s)
                except Exception as exc:  # noqa: BLE001 - a failed reprobe must not mask the real error
                    logger.debug("corrective reprobe failed for %s: %s", target.label, exc)
                else:
                    if actual != configured_platform:
                        logger.warning(
                            "%s: configured platform %r looks wrong for this host -- "
                            "probed %r instead",
                            target.label,
                            configured_platform,
                            actual,
                        )
                        result.platform = actual

        # Transports that are version-agnostic still report which one ran.
        if resolved is not None and result.qna_version is None:
            result.qna_version = resolved.version
        if not result.elapsed_ms:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    logger.info(
        "evaluating %d client-relevance expression(s) across %d target/version pair(s), "
        "max_parallel=%d",
        len(expressions),
        len(work),
        max_parallel,
    )

    # A queue rather than as_completed: a group now produces several results,
    # and the whole point of the streaming form is that each one is handed
    # over the moment it lands rather than when its group finishes.
    queue: asyncio.Queue[tuple[int, ClientRelevanceResult] | None] = asyncio.Queue()

    async def _run_group(base: int, target: Target, spec: str | None) -> None:
        def emit(offset: int, result: ClientRelevanceResult) -> None:
            queue.put_nowait((base + offset, result))

        try:
            await _group(target, spec, emit)
        finally:
            # One sentinel per group, so the drain below knows when every
            # group has had its say -- including a cancelled one.
            queue.put_nowait(None)

    tasks = [
        asyncio.create_task(_run_group(index * len(expressions), target, spec))
        for index, (target, spec) in enumerate(work)
    ]
    try:
        pending_groups = len(tasks)
        while pending_groups:
            item = await queue.get()
            if item is None:
                pending_groups -= 1
                continue
            yield item
    finally:
        # A consumer that stops early -- `break`, an exception, or an
        # abandoned generator -- would otherwise leave the remaining
        # evaluations running against live SSH connections and containers
        # with nobody to collect them.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def evaluate_client_relevance_stream(
    client_relevance: str,
    targets: Sequence[Target],
    *,
    qna_version: str | Sequence[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    pull_parallel: int = DEFAULT_PULL_PARALLEL,
    timeout_s: float = 30.0,
    transport_factory: TransportFactory | None = None,
    resolver: Resolver | None = None,
) -> AsyncIterator[ClientRelevanceResult]:
    """Evaluate ``client_relevance`` on every target, for every version.

    Yields each ``ClientRelevanceResult`` as soon as its (target, version)
    pair finishes, in completion order -- a slow SSH host never makes a fast
    local container wait. Use this for live progress; use
    :func:`evaluate_client_relevance` when you need the full set at once in
    target-then-version order (e.g. ``--diff``, which groups across all of
    them).
    """
    async for _index, result in _evaluate_stream_indexed(
        [client_relevance],
        targets,
        qna_version=qna_version,
        max_parallel=max_parallel,
        pull_parallel=pull_parallel,
        timeout_s=timeout_s,
        transport_factory=transport_factory,
        resolver=resolver,
    ):
        yield result


async def evaluate_client_relevance(
    client_relevance: str,
    targets: Sequence[Target],
    *,
    qna_version: str | Sequence[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    pull_parallel: int = DEFAULT_PULL_PARALLEL,
    timeout_s: float = 30.0,
    transport_factory: TransportFactory | None = None,
    resolver: Resolver | None = None,
) -> list[ClientRelevanceResult]:
    """Evaluate ``client_relevance`` on every target, for every version.

    Returns one result per (target, version) pair, in target-then-version
    order. Every failure mode is reported inside a result rather than raised.
    Waits for the whole fan-out; for incremental results as they arrive, use
    :func:`evaluate_client_relevance_stream`.
    """
    by_index: dict[int, ClientRelevanceResult] = {}
    async for index, result in _evaluate_stream_indexed(
        [client_relevance],
        targets,
        qna_version=qna_version,
        max_parallel=max_parallel,
        pull_parallel=pull_parallel,
        timeout_s=timeout_s,
        transport_factory=transport_factory,
        resolver=resolver,
    ):
        by_index[index] = result
    return [by_index[i] for i in range(len(by_index))]


async def evaluate_many_stream(
    expressions: Sequence[str],
    targets: Sequence[Target],
    *,
    qna_version: str | Sequence[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    pull_parallel: int = DEFAULT_PULL_PARALLEL,
    timeout_s: float = 30.0,
    transport_factory: TransportFactory | None = None,
    resolver: Resolver | None = None,
) -> AsyncIterator[ClientRelevanceResult]:
    """Evaluate every expression on every target, for every version.

    Yields each ``ClientRelevanceResult`` as soon as its cell finishes, in
    completion order. See :func:`evaluate_many` for what a batch buys.
    """
    async for _index, result in _evaluate_stream_indexed(
        expressions,
        targets,
        qna_version=qna_version,
        max_parallel=max_parallel,
        pull_parallel=pull_parallel,
        timeout_s=timeout_s,
        transport_factory=transport_factory,
        resolver=resolver,
    ):
        yield result


async def evaluate_many(
    expressions: Sequence[str],
    targets: Sequence[Target],
    *,
    qna_version: str | Sequence[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    pull_parallel: int = DEFAULT_PULL_PARALLEL,
    timeout_s: float = 30.0,
    transport_factory: TransportFactory | None = None,
    resolver: Resolver | None = None,
) -> list[ClientRelevanceResult]:
    """Evaluate several expressions across several targets in one pass.

    Returns one result per (target, version, expression), in that order. Each
    result carries the expression that produced it in ``client_relevance``, so
    no positional bookkeeping is needed to attribute an answer.

    What this saves over calling :func:`evaluate_client_relevance` once per
    expression is the per-expression *setup*: each (target, version) pair
    builds one transport, probes it, and prepares its image once, then runs
    every expression through it. For a container that is one image
    pull/prepared-image lookup and one ``docker run`` instead of N; for SSH it
    is one connection instead of N, since :class:`TransportSSH` multiplexes
    evaluations over the connection it already has. Container targets carrying
    more than one expression are kept alive for the batch automatically.

    Every failure mode is reported inside a result rather than raised, and a
    group that dies before evaluating anything still answers once per
    expression, so the result count always matches :func:`count_work`.
    """
    by_index: dict[int, ClientRelevanceResult] = {}
    async for index, result in _evaluate_stream_indexed(
        expressions,
        targets,
        qna_version=qna_version,
        max_parallel=max_parallel,
        pull_parallel=pull_parallel,
        timeout_s=timeout_s,
        transport_factory=transport_factory,
        resolver=resolver,
    ):
        by_index[index] = result
    return [by_index[i] for i in sorted(by_index)]


def worst_exit_code(results: Sequence[ClientRelevanceResult]) -> int:
    """Collapse a fan-out into one exit code, worst wins.

    Success is the absence of an error, not the presence of an answer: a plural
    inspector that legitimately matches nothing is a valid result and must not
    fail a CI gate.
    """
    if not results:
        return EXIT_QNA
    return max(_EXIT_BY_KIND.get(result.error_kind, EXIT_QNA) for result in results)


__all__ = [
    "DEFAULT_MAX_PARALLEL",
    "DEFAULT_PULL_PARALLEL",
    "EXIT_OK",
    "EXIT_QNA",
    "EXIT_RELEVANCE",
    "EXIT_RESOLVE",
    "EXIT_TRANSPORT",
    "Resolver",
    "Target",
    "TransportFactory",
    "count_work",
    "default_resolver",
    "default_transport_factory",
    "evaluate_client_relevance",
    "evaluate_client_relevance_stream",
    "evaluate_many",
    "evaluate_many_stream",
    "worst_exit_code",
]
