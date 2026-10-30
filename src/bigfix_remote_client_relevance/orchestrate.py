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
from typing import TYPE_CHECKING, Any, TypeGuard

from bigfix_remote_client_relevance.bootstrap.compat_memory import CompatMemory
from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError, fallback_floor
from bigfix_remote_client_relevance.bootstrap.targets import UnknownTargetError
from bigfix_remote_client_relevance.platform_support import can_run_processes, no_process_advice
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_RESOLVE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports import BatchTransport, FanOutTransport, Transport
from bigfix_remote_client_relevance.transports import ssh as ssh_transport
from bigfix_remote_client_relevance.transports.batch import chunk_expressions

if TYPE_CHECKING:
    from bigfix_remote_session_relevance import ConnectionConfig
    from bigfix_remote_session_relevance.backends.saas import SaasSessionPool

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

DEFAULT_BATCH_SIZE = 100
"""Most expressions one qna process answers in a batch. 368 took 70 ms on
one stdin (issue #9), so this bounds how much a failed batch has to redo
one at a time, not how fast a good one is."""

FANOUT_PARALLEL_EXPRESSIONS = 5
"""Most expressions a fan-out (Fast Query) target has in flight at once. Each
is its own query to a BigFix server shared by everyone, so a batch is run a
few at a time, not all at once."""


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

    connection: str | None = None
    """``fastquery`` only. Name of a REST or SaaS connection in the
    bigfix-remote-session-relevance config (``$BFRSR_CONFIG`` or
    ``~/.bigfix/.remote_session_connections.toml``)."""

    computer_ids: tuple[int, ...] = ()
    """``fastquery`` only. BigFix computer IDs to send the query to."""

    computer_query: str | None = None
    """``fastquery`` only. Session relevance selecting computers; the server
    resolves it with ``ids of (...)``. Merged with ``computer_ids``."""

    use_client_context: bool | None = None
    """``fastquery`` only. ``True`` evaluates in the agent's client context
    (fixlet and site inspectors available) instead of the default qna-like
    one. ``None`` leaves ``UseClientContext`` out of the request, for servers
    that predate it."""

    inventory_backed: bool = False
    """True only when this target was loaded from a `remote_clients.toml` the
    CLI can write back to (``--inventory`` with ``--update-inventory``, the
    default -- set by the CLI, never by :func:`~...inventory.load_inventory`
    itself). Meaningless for every kind except ``online_evaluator``: unlike
    ``local`` (a free ``sys.platform`` check) or ``ssh`` (probed anyway while
    resolving a version), its arch probe is a real network round trip whose
    only purpose is a value to persist -- see
    ``_one()``'s arch-probe block. False for every ad hoc target (CLI flags,
    or a `Target` built directly by a library caller) and for an inventory
    host loaded with ``--no-update-inventory``, since a probed value would
    just be thrown away there.
    """

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


def default_transport_factory(
    target: Target,
    *,
    coordinator: object | None = None,
    saas_sessions: SaasSessionPool | None = None,
) -> Transport:
    """Build the transport a target calls for.

    ``coordinator`` is how a fan-out shares image work between the transports
    it builds, and ``saas_sessions`` how its SaaS Fast Query targets share one
    sign-in per account; both are keyword-only and optional so the
    one-argument :data:`TransportFactory` signature still holds.
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
        from bigfix_remote_session_relevance import RestBackend, SaasBackend, TrustStore

        from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery

        conn = require_fastquery_connection(target.connection)
        backend_class = SaasBackend if conn.backend == "saas" else RestBackend
        # Not entered as a context manager: the transport owns it from here,
        # and the fan-out's _release closes it through TransportFastQuery.aclose.
        # The transport sees a SaasBackend and sends one query per computer.
        backend = backend_class.from_config(conn, trust=TrustStore())
        if isinstance(backend, SaasBackend) and saas_sessions is not None:
            # One login and one cached failure per account, not per target:
            # 100 failed SaaS logins lock the account for good.
            saas_sessions.attach(backend)
        return TransportFastQuery(
            backend,
            computer_ids=target.computer_ids,
            computer_query=target.computer_query,
            use_client_context=target.use_client_context,
            host=target.name,
        )
    if target.kind == "online_evaluator":
        from bigfix_remote_client_relevance.transports.online_evaluator import (
            TransportOnlineEvaluator,
        )

        if not target.base_url:
            raise ValueError("online_evaluator target needs base_url")
        return TransportOnlineEvaluator(target.base_url, host=target.name)
    raise ValueError(f"unknown target kind {target.kind!r}")


SETUP_FASTQUERY_ADVICE = "run --setup-fastquery to add a REST or SaaS connection"

# Backends with bigfix-remote-session-relevance's ClientQuery API. SaaS
# (since its 1.3.0) takes one computer per query, so TransportFastQuery sends
# it one query per computer.
FASTQUERY_BACKENDS = frozenset({"rest", "saas"})


def require_fastquery_connection(name: str | None) -> ConnectionConfig:
    """The enabled connection called ``name``, if Fast Query can use it.

    Otherwise raises ``ValueError`` saying which of unknown, disabled, or
    the wrong backend it is: "none found" for a SaaS connection that plainly
    exists sent the user hunting for a config problem they did not have.
    """
    from bigfix_remote_session_relevance import ConfigError, load_config

    if not name:
        raise ValueError(f"Fast Query needs a connection; {SETUP_FASTQUERY_ADVICE}")
    try:
        config = load_config()
    except ConfigError as exc:
        # No setup advice: the wizard would have to read this same file, so
        # the cause is the whole message -- fixing the file is the fix.
        raise ValueError(f"cannot read the connection config for Fast Query ({exc})") from exc
    try:
        conn = config.connection(name)
    except ConfigError as exc:
        logger.debug("no Fast Query connection %r: %s", name, exc)
        raise ValueError(f"no connection named {name!r}; {SETUP_FASTQUERY_ADVICE}") from exc
    if not conn.enabled:
        raise ValueError(f"connection {name!r} is disabled; enable it to use Fast Query")
    if conn.backend not in FASTQUERY_BACKENDS:
        raise ValueError(
            f"connection {name!r} is a {conn.backend} connection, and Fast Query "
            f"only works through REST or SaaS; {SETUP_FASTQUERY_ADVICE}"
        )
    return conn


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


MAX_FALLBACK_STEPS = 3
"""How many older releases a too-new build may step back through.

Enough to cross a release that dropped a platform's build, without walking
the whole release history for a target nothing will ever start on."""

PreviousVersion = Callable[[str], Coroutine[Any, Any, str | None]]


async def default_previous_version(version: str) -> str | None:
    """The release before ``version``, per the BigFix release index."""
    from bigfix_remote_client_relevance.bootstrap.release_site import (
        previous_version as release_previous_version,
    )

    return await asyncio.to_thread(release_previous_version, version)


def _memory_key(target: Target) -> str:
    """What a too-new verdict is remembered against: where qna runs, and as what."""
    return f"{target.kind}:{target.image or target.name}:{target.platform}@{target.arch}"


def _split_floor(target: Target, version: str) -> str | None:
    from bigfix_remote_client_relevance.bootstrap.targets import spec_for

    if target.platform is None or target.arch is None:
        return None
    try:
        release_platform = spec_for(target.platform).release_platform
    except Exception:  # noqa: BLE001 - an unknown platform just has no known split
        return None
    return fallback_floor(version, platform=release_platform, arch=target.arch)


def _is_too_new(result: ClientRelevanceResult) -> bool:
    """Whether a result failed because the qna build needs a newer runtime."""
    from bigfix_remote_client_relevance.transports.container_libs import (
        incompatible_symbol_versions,
    )

    return result.error_kind == ERROR_KIND_BOOTSTRAP and bool(
        incompatible_symbol_versions(result.error or "")
    )


UNPINNABLE_KINDS = frozenset({"fastquery", "online_evaluator"})
"""Transports that evaluate with an agent this tool does not supply, so a
qna version spec means nothing to them."""


def _version_specs(target: Target, run_wide: str | Sequence[str] | None) -> list[str | None]:
    """Which version specs apply to this target, per-target override winning."""
    if target.kind in UNPINNABLE_KINDS and target.qna_version is None:
        # A run-wide spec is for the hosts that download qna; failing every
        # fixed-agent host in a mixed run, every run, told the user nothing.
        # A spec set on the target itself still reaches _group and errors.
        return [None]
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
    """How many results a fan-out over these arguments will produce, at least.

    Exposed because a streaming caller has to decide how to format its first
    result before it knows how many follow, and this is not simply
    ``len(targets)``: per-host ``qna_version`` overrides mean each target can
    contribute a different number of pairs, and a batch multiplies each pair by
    its expressions.

    Exact unless a target's transport is a
    :class:`~.transports.FanOutTransport` (``fastquery``): such a cell yields
    one result per endpoint that answers, which is only known once it has,
    so there the count is a floor of one per cell.
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

    Optional by design: ``local`` holds nothing that needs releasing and
    exposes no ``aclose``. A failure here is logged, never
    raised -- a run that produced its answers must not fail on cleanup.
    """
    closer = getattr(transport, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 - cleanup never fails a run
        logger.debug("releasing a transport failed: %s", exc)


def _class_implements(cls: type, protocol: type) -> bool:
    """The class half of ``isinstance`` against a runtime_checkable Protocol."""
    return issubclass(cls, protocol)


# The one method each optional transport protocol is made of.
_PROTOCOL_METHOD: dict[type, str] = {
    FanOutTransport: "evaluate_client_relevance_fanout",
    BatchTransport: "evaluate_client_relevance_batch",
}


class _Capabilities:
    """Which optional protocols a run's transports implement, asked per target.

    ``isinstance`` against a runtime_checkable Protocol is fast when the class
    implements it (``ABCMeta`` caches that), but every miss walks the
    protocol's members with ``inspect.getattr_static``: ~1.8 us on 3.12+, and
    most transports miss at least one of these, on every target of every
    run. So each class's answer is worked out once per run -- not per
    process, since tests and callers patch transport classes -- and only the
    instance ``__dict__`` is looked at per target, which is all the rest of
    ``isinstance`` (3.12+) reads for these one-method protocols.
    """

    def __init__(self) -> None:
        self._by_class: dict[tuple[type, type], bool] = {}

    def fan_out(self, transport: object) -> TypeGuard[FanOutTransport]:
        return self._supports(transport, FanOutTransport)

    def batch(self, transport: object) -> TypeGuard[BatchTransport]:
        return self._supports(transport, BatchTransport)

    def _supports(self, transport: object, protocol: type) -> bool:
        if self._class_supports(type(transport), protocol):
            return True
        # isinstance asks __class__ too, which a spec'd mock reports as its spec.
        reported = transport.__class__
        if reported is not type(transport) and self._class_supports(reported, protocol):
            return True
        # Like getattr_static: a method assigned on the instance counts, and
        # __getattr__ (a MagicMock's, a proxy's) never runs.
        try:
            attrs = object.__getattribute__(transport, "__dict__")
        except AttributeError:
            return False
        return attrs.get(_PROTOCOL_METHOD[protocol]) is not None

    def _class_supports(self, cls: type, protocol: type) -> bool:
        key = (cls, protocol)
        known = self._by_class.get(key)
        if known is None:
            known = self._by_class[key] = _class_implements(cls, protocol)
        return known


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
    previous_version: PreviousVersion | None = None,
    compat_memory: CompatMemory | None = None,
    batch: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> AsyncIterator[tuple[tuple[int, int], ClientRelevanceResult]]:
    """Shared core: yield ``((work_index, sub), result)`` as each result lands.

    The unit of *work* is a **group** -- one (target, version) pair -- and the
    unit of *result* is a cell, one expression within a group. A group builds
    its transport, probes, and prepares once, then runs every expression
    through that one transport in sequence: that is what makes a batch cheaper
    than the same expressions run separately, and for a container it is the
    difference between one `docker run` and N.

    ``work_index`` is the cell's position in target-then-version-then-expression
    order, kept so callers that need that ordering
    (:func:`evaluate_client_relevance`) can reconstruct it even though
    completion order is whatever finishes first. ``sub`` is 0 except for a
    :class:`~.transports.FanOutTransport`, whose one cell yields a result per
    endpoint numbered from 0 -- so sorting the keys gives
    expression-then-host order with nothing overwritten. Every failure mode
    is reported inside a result rather than raised.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")

    if transport_factory is None:
        # One coordinator per run, so the transports built below share image
        # work with each other and with nothing else.
        from bigfix_remote_client_relevance.transports.coordination import ImageCoordinator

        coordinator = ImageCoordinator()
        # Likewise one SaaS session pool per run (not per process: a session
        # is tied to the run's event loop and should not outlive it).
        from bigfix_remote_session_relevance.backends.saas import SaasSessionPool

        saas_sessions = SaasSessionPool()

        def _default_transport_factory(target: Target) -> Transport:
            return default_transport_factory(
                target, coordinator=coordinator, saas_sessions=saas_sessions
            )

        transport_factory = _default_transport_factory

    resolver = resolver or default_resolver
    previous_version = previous_version or default_previous_version
    memory = compat_memory or CompatMemory()
    semaphore = asyncio.Semaphore(max_parallel)
    capabilities = _Capabilities()
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
            # What was requested for the target, as on the transport's own
            # failures: a host that failed before submitting still says
            # which context it asked for.
            use_client_context=target.use_client_context if target.kind == "fastquery" else None,
        )

    def _group_failure(target: Target, kind: str, message: str) -> list[ClientRelevanceResult]:
        """The same failure, once per expression.

        A group that never gets as far as evaluating still owes one result per
        cell: ``count_work`` is the denominator (a floor, for fan-out
        targets) a progress indicator commits to before the first result
        arrives, and a short run would silently misreport rather than fail.
        """
        return [_failure(target, kind, message, expression) for expression in expressions]

    async def _resolve(target: Target, spec: str | None) -> ResolvedQna:
        # One task per (spec, arch): targets sharing both share the download.
        key = (spec, f"{target.platform or target.kind}-{target.arch}")
        if key not in resolutions:
            resolutions[key] = asyncio.create_task(resolver(spec, target))
        return await resolutions[key]

    async def _group(target: Target, spec: str | None, emit: Callable[..., None]) -> None:
        """Evaluate every expression against one (target, version) pair."""
        resolved: ResolvedQna | None = None
        # Captured before any probe-before-resolve replaces target.platform,
        # so the corrective reprobe below only fires for a platform the
        # caller actually set (never for one this run just probed itself).
        configured_platform = target.platform

        # Before the transport, the probes and above all the resolver: on iOS
        # that resolver downloaded an agent package nothing could run. A
        # pinned ssh/container target with its platform already known has no
        # probe to fail first, so the probe can't be relied on to stop it.
        # This one is about the platform, whoever builds the transport: no
        # local or container transport can start qna there. (The ssh check,
        # below, is about this library's own transport's dependency instead.)
        if target.kind in ("local", "container") and not can_run_processes():
            unavailable = no_process_advice(f"the {target.kind} transport")
            for index, result in enumerate(
                _group_failure(target, ERROR_KIND_TRANSPORT, unavailable)
            ):
                emit(index, result)
            return

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

        # Still before any probe or resolve. Only this library's TransportSSH
        # needs asyncssh, however it was reached (the default factory, or
        # default_transport_factory passed or wrapped by a caller). Checking
        # by the transport built, not the target's kind, spares a caller's own
        # ssh-kind transport the asyncssh import (~65 ms on a process's first
        # run in 1.10.0) and the refusal where asyncssh is missing. Building a
        # TransportSSH doesn't import asyncssh; connecting does.
        if isinstance(transport, ssh_transport.TransportSSH):
            ssh_missing = ssh_transport.ssh_unavailable_reason()
            if ssh_missing is not None:
                for index, result in enumerate(
                    _group_failure(target, ERROR_KIND_TRANSPORT, f"{target.label}: {ssh_missing}")
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
                        "endpoints evaluate with their installed BES agent; "
                        "remove qna_version from this host",
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
                        "environment; remove qna_version from this host",
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
            #
            # online_evaluator is different again: unlike ssh (whose probe
            # earns its cost by picking the right artifact), its probe is a
            # real network round trip whose only payoff is a value to display
            # and persist -- there is no version to resolve (it refuses a spec
            # earlier and returns before this point), so `spec is not None`
            # would never gate it the way it gates ssh. Paying for that round
            # trip is only worth it when the result can actually be written
            # back, so `inventory_backed` (set by the CLI only for
            # `--inventory` hosts under `--update-inventory`) is the gate
            # instead -- an ad hoc `--online-evaluator URL` never probes at
            # all. The probe itself is relevance ("architecture of the
            # operating system" -- see probe_arch_via_relevance) sent as an
            # ordinary evaluation, since there is no other side channel.
            #
            # fastquery never probes: one target is many endpoints, so there
            # is no single arch to find or persist.
            arch_probe = getattr(transport, "resolve_arch", None)
            if (
                target.arch is None
                and arch_probe is not None
                and (
                    spec is not None
                    or target.kind == "local"
                    or (target.kind == "online_evaluator" and target.inventory_backed)
                )
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

            async def _prepare(qna: ResolvedQna | None) -> None:
                if prepare is None:
                    return
                # Pulling and building under their own budget. Failures are not
                # fatal: this is an optimization, and the evaluation below hits the
                # same code path and reports the failure in its own vocabulary.
                try:
                    async with image_budget:
                        await prepare(qna=qna, timeout_s=timeout_s)
                except Exception as exc:  # noqa: BLE001 - the evaluation will report it
                    logger.debug("image preparation failed for %s: %s", target.label, exc)

            async def _evaluate(expression: str, qna: ResolvedQna | None) -> ClientRelevanceResult:
                return await _evaluate_one(
                    transport,
                    target,
                    expression,
                    resolved=qna,
                    spec=spec,
                    configured_platform=configured_platform,
                )

            if not expressions:
                await _prepare(resolved)
                return

            if capabilities.fan_out(transport):
                # One cell, many endpoints. Nothing to fall back through:
                # fan-out transports evaluate with whatever each endpoint has.
                # Batching them is running a few at once; waiting for a lane
                # happens before the transport starts its clock, so a queued
                # expression does not spend its timeout in the queue.
                # A transport can ask for fewer (see FanOutTransport).
                parallel = getattr(transport, "parallel_expressions", None)
                lanes = asyncio.Semaphore((parallel or FANOUT_PARALLEL_EXPRESSIONS) if batch else 1)

                async def _cell(index: int, expression: str) -> None:
                    async with lanes:
                        cell = await _evaluate_fanout(transport, target, expression)
                    for sub, result in enumerate(cell):
                        emit(index, result, sub)

                await asyncio.gather(*(_cell(i, e) for i, e in enumerate(expressions)))
                return

            # A build already known to be too new here is skipped outright, so
            # an old-runtime target pays for the failed attempt once, not on
            # every run.
            memory_key = _memory_key(target)
            requested = resolved
            remembered = memory.lookup(memory_key, resolved.version) if resolved else None
            if remembered is not None:
                try:
                    resolved = await _resolve(target, remembered)
                except Exception as exc:  # noqa: BLE001 - fall back to the requested build
                    logger.debug("remembered %s unusable for %s: %s", remembered, target.label, exc)
                    remembered = None
                else:
                    logger.debug(
                        "%s: qna %s is known to be too new here; using %s",
                        target.label,
                        requested.version if requested else None,
                        resolved.version,
                    )

            await _prepare(resolved)
            first = await _evaluate(expressions[0], resolved)
            if resolved is not None and _is_too_new(first):
                if remembered is not None:
                    memory.forget(memory_key)
                older = await _fall_back(target, resolved, _prepare, _evaluate, expressions[0])
                if older is not None:
                    resolved, first = older

            fallback_from: str | None = None
            if (
                requested is not None
                and resolved is not None
                and resolved.version != requested.version
            ):
                fallback_from = requested.version
                if remembered is None or resolved.version != remembered:
                    memory.record(memory_key, too_new=requested.version, works=resolved.version)

            def _tagged(result: ClientRelevanceResult) -> ClientRelevanceResult:
                result.qna_fallback_from = fallback_from
                return result

            emit(0, _tagged(first))
            # The first expression ran alone: it is what found out whether
            # the build starts here at all. The rest share qna processes, a
            # chunk at a time, when the transport can.
            batcher = transport if batch and capabilities.batch(transport) else None
            index = 1
            for chunk in chunk_expressions(
                expressions[1:], max_count=batch_size if batcher is not None else 1
            ):
                batched = None
                if batcher is not None and len(chunk) > 1:
                    batched = await _evaluate_batch(batcher, target, chunk, resolved)
                    if batched is None:
                        # Whatever stopped this chunk -- expressions too slow
                        # for one timeout_s, a qna that cannot run here --
                        # would most likely stop the next one too, at the
                        # cost of another timeout every run. One failed chunk
                        # per group is the most batching may cost.
                        batcher = None
                if batched is not None:
                    for result in batched:
                        emit(index, _tagged(result))
                        index += 1
                    continue
                for expression in chunk:
                    emit(index, _tagged(await _evaluate(expression, resolved)))
                    index += 1
        finally:
            # Nothing else in the process holds this transport, so an
            # unreleased one leaks whatever it opened -- an SSH connection,
            # or (worse, because it outlives the process) a kept-alive
            # container.
            await _release(transport)

    async def _fall_back(
        target: Target,
        too_new: ResolvedQna,
        prepare: Callable[[ResolvedQna], Coroutine[Any, Any, None]],
        evaluate: Callable[[str, ResolvedQna], Coroutine[Any, Any, ClientRelevanceResult]],
        expression: str,
    ) -> tuple[ResolvedQna, ClientRelevanceResult] | None:
        """Step back through older releases until one's build starts on ``target``.

        Bounded by :data:`MAX_FALLBACK_STEPS`; a release with no artifact for
        this platform/arch counts as a step and is skipped. ``None`` when none
        starts, so the caller reports the original failure, which names the
        version actually asked for.
        """
        version = too_new.version
        # Past a known build split, the first step clears it entirely.
        floor = _split_floor(target, version)
        if floor is not None:
            version = floor
        for _ in range(MAX_FALLBACK_STEPS):
            try:
                older = await previous_version(version)
            except Exception as exc:  # noqa: BLE001 - keep the original failure
                logger.debug("could not step back from %s: %s", version, exc)
                return None
            if older is None:
                return None
            version = older
            try:
                candidate = await _resolve(target, older)
            except Exception as exc:  # noqa: BLE001 - a missing artifact is just skipped
                logger.debug("skipping %s for %s: %s", older, target.label, exc)
                continue
            await prepare(candidate)
            result = await evaluate(expression, candidate)
            if not _is_too_new(result):
                # INFO, not WARNING: the result label already says so, and the
                # outcome is remembered, so this prints once per target.
                logger.info(
                    "%s: qna %s is too new for this target; fell back to %s",
                    target.label,
                    too_new.version,
                    candidate.version,
                )
                return candidate, result
        return None

    async def _evaluate_fanout(
        transport: FanOutTransport, target: Target, client_relevance: str
    ) -> list[ClientRelevanceResult]:
        """One expression against a fan-out transport: at least one result."""
        started = time.monotonic()
        async with semaphore:
            try:
                results = await transport.evaluate_client_relevance_fanout(
                    client_relevance, timeout_s=timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - one bad cell never fails the run
                logger.debug("transport failed for %s: %s", target.label, exc)
                results = []
                message = f"{target.label}: {exc}"
            else:
                message = f"{target.label}: no results"
        if not results:
            # count_work promised at least one result for this cell.
            return [_failure(target, ERROR_KIND_TRANSPORT, message, client_relevance)]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        for result in results:
            # No platform/arch stamping, unlike _evaluate_one: the target is
            # many endpoints, so whatever it carries describes none of them.
            if not result.elapsed_ms:
                result.elapsed_ms = elapsed_ms
        return results

    def _stamp(
        result: ClientRelevanceResult,
        target: Target,
        resolved: ResolvedQna | None,
        started: float,
    ) -> ClientRelevanceResult:
        """What every qna result gets from the run that produced it."""
        # Whatever platform/arch this run actually used -- explicit, freshly
        # probed, or (platform only, in _evaluate_one) corrected -- so the CLI
        # can write them back.
        result.platform = target.platform
        # Local/ssh/container always have a concrete target.arch by this point
        # (an explicit value, or a fresh probe above); online_evaluator and
        # fastquery have neither, so overwriting with an unset target.arch
        # would clobber a transport-reported label for no reason -- e.g.
        # online_evaluator's own fixed, decorative "x86_64" (see
        # TransportOnlineEvaluator). Trust the transport's own arch whenever
        # the target never had one to begin with.
        if target.arch is not None:
            result.arch = target.arch
        # Transports that are version-agnostic still report which one ran.
        if resolved is not None and result.qna_version is None:
            result.qna_version = resolved.version
        if not result.elapsed_ms:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _evaluate_batch(
        transport: BatchTransport,
        target: Target,
        chunk: list[str],
        resolved: ResolvedQna | None,
    ) -> list[ClientRelevanceResult] | None:
        """A chunk through one qna process; ``None`` to evaluate it one at a time.

        One semaphore slot for the whole chunk: it is one process. Anything
        short of exactly one result per expression is not trusted -- the
        serial path reports every failure in its own words anyway.
        """
        started = time.monotonic()
        async with semaphore:
            try:
                results = await transport.evaluate_client_relevance_batch(
                    chunk, qna=resolved, timeout_s=timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - the serial path will report it
                logger.debug("batch failed for %s: %s", target.label, exc)
                results = None
        if results is None or len(results) != len(chunk):
            # DEBUG: a target that cannot batch will say so on every run.
            logger.debug(
                "%s: a batch of %d did not run cleanly; evaluating one at a time",
                target.label,
                len(chunk),
            )
            return None
        return [_stamp(result, target, resolved, started) for result in results]

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

        _stamp(result, target, resolved, started)

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
    queue: asyncio.Queue[tuple[tuple[int, int], ClientRelevanceResult] | None] = asyncio.Queue()

    async def _run_group(base: int, target: Target, spec: str | None) -> None:
        def emit(offset: int, result: ClientRelevanceResult, sub: int = 0) -> None:
            queue.put_nowait(((base + offset, sub), result))

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
    previous_version: PreviousVersion | None = None,
    compat_memory: CompatMemory | None = None,
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
        previous_version=previous_version,
        compat_memory=compat_memory,
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
    previous_version: PreviousVersion | None = None,
    compat_memory: CompatMemory | None = None,
) -> list[ClientRelevanceResult]:
    """Evaluate ``client_relevance`` on every target, for every version.

    Returns one result per (target, version) pair, in target-then-version
    order -- or, for a fan-out target such as Fast Query, one per endpoint
    that pair reached, in host order. Every failure mode is reported inside a
    result rather than raised.
    Waits for the whole fan-out; for incremental results as they arrive, use
    :func:`evaluate_client_relevance_stream`.
    """
    by_index: dict[tuple[int, int], ClientRelevanceResult] = {}
    async for index, result in _evaluate_stream_indexed(
        [client_relevance],
        targets,
        qna_version=qna_version,
        max_parallel=max_parallel,
        pull_parallel=pull_parallel,
        timeout_s=timeout_s,
        transport_factory=transport_factory,
        resolver=resolver,
        previous_version=previous_version,
        compat_memory=compat_memory,
    ):
        by_index[index] = result
    return [by_index[key] for key in sorted(by_index)]


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
    previous_version: PreviousVersion | None = None,
    compat_memory: CompatMemory | None = None,
    batch: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
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
        previous_version=previous_version,
        compat_memory=compat_memory,
        batch=batch,
        batch_size=batch_size,
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
    previous_version: PreviousVersion | None = None,
    compat_memory: CompatMemory | None = None,
    batch: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
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

    ``batch`` (on by default) also shares the qna *process*: after the first
    expression, which runs alone because it is what detects a build too new
    for the target, the rest go through one qna stdin per ``batch_size``
    chunk, on transports that support it (local, ssh, container). A chunk
    that does not run cleanly -- a timeout within ``timeout_s`` for the whole
    chunk, a crash, output that does not line up -- is evaluated again one
    expression at a time, and so is the rest of that target's work, so
    batching changes how fast results arrive, never what they say. Batched results carry ``qna_batch_size``, and their
    ``elapsed_ms`` is the whole qna run's. A fan-out target (Fast Query)
    instead runs up to :data:`FANOUT_PARALLEL_EXPRESSIONS` expressions at
    once, each still its own query. ``batch=False`` evaluates everything one
    expression at a time, as before.

    Every failure mode is reported inside a result rather than raised, and a
    group that dies before evaluating anything still answers once per
    expression, so the result count matches :func:`count_work` -- exactly,
    except that a fan-out target (Fast Query) adds a result per endpoint
    beyond the first, so there it is at least that count.
    """
    by_index: dict[tuple[int, int], ClientRelevanceResult] = {}
    async for index, result in _evaluate_stream_indexed(
        expressions,
        targets,
        qna_version=qna_version,
        max_parallel=max_parallel,
        pull_parallel=pull_parallel,
        timeout_s=timeout_s,
        transport_factory=transport_factory,
        resolver=resolver,
        previous_version=previous_version,
        compat_memory=compat_memory,
        batch=batch,
        batch_size=batch_size,
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
    "PreviousVersion",
    "Resolver",
    "Target",
    "TransportFactory",
    "count_work",
    "default_previous_version",
    "default_resolver",
    "default_transport_factory",
    "evaluate_client_relevance",
    "evaluate_client_relevance_stream",
    "evaluate_many",
    "evaluate_many_stream",
    "worst_exit_code",
]
