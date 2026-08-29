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
    """``"ssh"`` | ``"local"`` | ``"container"`` | ``"fastquery"``."""

    name: str
    """SSH alias, image name, or ``"local"``."""

    user: str | None = None
    become: bool | None = None
    """None means unspecified: `default_transport_factory` decides -- True for
    `local` on a macOS controller (qna needs root there unconditionally),
    False otherwise. An explicit True/False always wins."""
    image: str | None = None
    arch: str = "x86_64"
    platform: str | None = None
    """Bootstrap target key (``"ubuntu"``, ``"macos"``, ...); probed if None."""

    qna_version: str | Sequence[str] | None = None
    """Per-target override of the run-wide version spec."""

    keep_alive: bool = False
    rebuild_image: bool = False
    """Container only. Force a fresh prepared image instead of reusing a cached one."""

    auto_setup: bool = True
    """Container only. Install runtime libraries the image is missing. Off for air-gapped hosts."""

    verify_host_key: bool = True
    """SSH only. Off removes protection against interception; lab hosts only."""

    extra: dict[str, object] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.kind == "container":
            return f"container:{self.image or self.name}@{self.arch}"
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
        return TransportLocal(target=target.platform, become=become)
    if target.kind == "ssh":
        from bigfix_remote_client_relevance.transports.ssh import TransportSSH

        return TransportSSH(
            target.name,
            user=target.user,
            become=bool(target.become),
            target=target.platform,
            verify_host_key=target.verify_host_key,
        )
    if target.kind == "container":
        from bigfix_remote_client_relevance.transports.container import TransportContainer

        return TransportContainer(
            target.image or target.name,
            arch=target.arch,
            keep_alive=target.keep_alive,
            target=target.platform,
            rebuild_image=target.rebuild_image,
            auto_setup=target.auto_setup,
            coordinator=coordinator,  # type: ignore[arg-type]
        )
    if target.kind == "fastquery":
        from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery

        return TransportFastQuery(target.extra.get("besapi_client"))
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

    version = await asyncio.to_thread(resolve_version_spec, spec)
    ref = await asyncio.to_thread(
        artifact_for, version, platform=release_platform, arch=target.arch
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


def count_work(targets: Sequence[Target], qna_version: str | Sequence[str] | None = None) -> int:
    """How many results a fan-out over these arguments will produce.

    Exposed because a streaming caller has to decide how to format its first
    result before it knows how many follow, and this is not simply
    ``len(targets)``: per-host ``qna_version`` overrides mean each target can
    contribute a different number of pairs.
    """
    return sum(len(_version_specs(target, qna_version)) for target in targets)


async def _evaluate_stream_indexed(
    client_relevance: str,
    targets: Sequence[Target],
    *,
    qna_version: str | Sequence[str] | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    pull_parallel: int = DEFAULT_PULL_PARALLEL,
    timeout_s: float = 30.0,
    transport_factory: TransportFactory | None = None,
    resolver: Resolver | None = None,
) -> AsyncIterator[tuple[int, ClientRelevanceResult]]:
    """Shared core: yield ``(work_index, result)`` as each pair finishes.

    ``work_index`` is the pair's position in target-then-version order, kept
    so callers that need that ordering (:func:`evaluate_client_relevance`) can
    reconstruct it even though completion order is whatever finishes first.
    Every failure mode is reported inside a result rather than raised.
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

    def _failure(target: Target, kind: str, message: str) -> ClientRelevanceResult:
        return ClientRelevanceResult(
            host=target.label,
            transport=target.kind,
            client_relevance=client_relevance,
            error=message,
            error_kind=kind,
        )

    async def _resolve(target: Target, spec: str | None) -> ResolvedQna:
        # One task per (spec, arch): targets sharing both share the download.
        key = (spec, f"{target.platform or target.kind}-{target.arch}")
        if key not in resolutions:
            resolutions[key] = asyncio.create_task(resolver(spec, target))
        return await resolutions[key]

    async def _one(target: Target, spec: str | None) -> ClientRelevanceResult:
        started = time.monotonic()
        resolved: ResolvedQna | None = None
        # Captured before any probe-before-resolve replaces target.platform,
        # so the corrective reprobe below only fires for a platform the
        # caller actually set (never for one this run just probed itself).
        configured_platform = target.platform

        try:
            transport = transport_factory(target)
        except Exception as exc:  # noqa: BLE001 - one bad target never fails the run
            logger.debug("transport construction failed for %s: %s", target.label, exc)
            return _failure(target, ERROR_KIND_TRANSPORT, f"{target.label}: {exc}")

        prepare = getattr(transport, "prepare", None)
        # Only image-backed transports have an image phase to budget; an SSH
        # sweep must not be serialized by a container-pull limit.
        image_budget: AbstractAsyncContextManager[Any] = (
            image_semaphore if prepare is not None else contextlib.nullcontext()
        )

        if spec is not None:
            if target.kind == "fastquery":
                # Endpoints evaluate with their installed agent; refuse before
                # doing any work rather than resolving a version nothing can use.
                return _failure(
                    target,
                    ERROR_KIND_RESOLVE,
                    f"cannot pin qna version {spec!r} for the fastquery transport: "
                    "endpoints evaluate with their installed BES agent",
                )
            # The resolver picks the artifact (deb vs rpm) from the platform, so
            # an unset one must be probed BEFORE resolution. For a container the
            # probe can trigger the image pull, so it shares the image budget.
            probe = getattr(transport, "resolve_platform", None)
            if target.platform is None and probe is not None:
                try:
                    async with image_budget:
                        probed = await probe(timeout_s=timeout_s)
                    target = dataclasses.replace(target, platform=probed)
                except UnknownTargetError as exc:
                    return _failure(target, ERROR_KIND_BOOTSTRAP, str(exc))
                except Exception as exc:  # noqa: BLE001 - a bad probe never kills a run
                    logger.debug("platform probe failed for %s: %s", target.label, exc)
                    return _failure(target, ERROR_KIND_TRANSPORT, f"{target.label}: {exc}")
            try:
                resolved = await _resolve(target, spec)
            except ResolveError as exc:
                return _failure(target, ERROR_KIND_RESOLVE, str(exc))
            except Exception as exc:  # noqa: BLE001 - resolution never kills a run
                logger.debug("resolution failed for %s: %s", spec, exc)
                return _failure(target, ERROR_KIND_RESOLVE, f"could not resolve {spec!r}: {exc}")

        if prepare is not None:
            # Pulling and building under their own budget. Failures are not
            # fatal: this is an optimization, and the evaluation below hits the
            # same code path and reports the failure in its own vocabulary.
            try:
                async with image_budget:
                    await prepare(qna=resolved, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001 - the evaluation will report it
                logger.debug("image preparation failed for %s: %s", target.label, exc)

        async with semaphore:
            try:
                result = await transport.evaluate_client_relevance(
                    client_relevance, qna=resolved, timeout_s=timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - one bad target never fails the run
                logger.debug("transport failed for %s: %s", target.label, exc)
                return _failure(target, ERROR_KIND_TRANSPORT, f"{target.label}: {exc}")

        # Whatever platform this run actually used -- explicit, freshly
        # probed, or (below) corrected -- so the CLI can write it back.
        result.platform = target.platform

        if spec is not None and configured_platform is not None:
            # An explicit platform was trusted outright above and never
            # probed. If it made the run fail, that's exactly the failure
            # mode a wrong hosts.toml entry produces (issue: a stale or
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
        "evaluating client relevance across %d target/version pair(s), max_parallel=%d",
        len(work),
        max_parallel,
    )

    async def _one_indexed(
        index: int, target: Target, spec: str | None
    ) -> tuple[int, ClientRelevanceResult]:
        return index, await _one(target, spec)

    # Plain tasks + as_completed, not a TaskGroup: TaskGroup only releases its
    # results once every task has finished, which is exactly the "all at
    # once" behavior a streaming caller wants to avoid. This is safe because
    # _one() never lets an exception escape -- every failure mode above comes
    # back as a result, not a raise -- so there is nothing here for
    # TaskGroup's cancel-siblings-on-error behavior to actually add.
    tasks = [
        asyncio.create_task(_one_indexed(index, target, spec))
        for index, (target, spec) in enumerate(work)
    ]
    try:
        for coro in asyncio.as_completed(tasks):
            yield await coro
    finally:
        # A consumer that stops early -- `break`, an exception, or an
        # abandoned generator -- would otherwise leave the remaining
        # evaluations running against live SSH connections and containers
        # with nobody to collect them. TaskGroup gave us this for free; on
        # plain tasks it has to be written out.
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
        client_relevance,
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
        client_relevance,
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
    "worst_exit_code",
]
