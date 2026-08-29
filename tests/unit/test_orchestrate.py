"""Tests for the targets x versions fan-out.

orchestrate.py is the single entry point the CLI and the future MCP tool both
sit on, so its contract matters more than most: one result per (target,
version), bounded concurrency, and one version resolution per run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bigfix_remote_client_relevance.orchestrate import (
    EXIT_OK,
    EXIT_QNA,
    EXIT_RELEVANCE,
    EXIT_RESOLVE,
    EXIT_TRANSPORT,
    Target,
    count_work,
    evaluate_client_relevance,
    evaluate_client_relevance_stream,
    worst_exit_code,
)
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_RESOLVE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
)


class FakeTransport:
    """Records calls and reports peak concurrency."""

    def __init__(self, host: str, tracker: dict | None = None, delay: float = 0.0) -> None:
        self.host = host
        self.calls: list[ResolvedQna | None] = []
        self._tracker = tracker if tracker is not None else {"live": 0, "peak": 0}
        self._delay = delay

    async def evaluate_client_relevance(
        self, client_relevance, *, qna_path=None, qna=None, timeout_s=30.0
    ):
        self._tracker["live"] += 1
        self._tracker["peak"] = max(self._tracker["peak"], self._tracker["live"])
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            self.calls.append(qna)
            return ClientRelevanceResult(
                host=self.host,
                transport="fake",
                client_relevance=client_relevance,
                answers=["yes"],
                qna_version=qna.version if qna else None,
            )
        finally:
            self._tracker["live"] -= 1


def make_resolver(mapping=None, counter=None):
    """A resolver that records how many times it was asked."""

    async def resolve(spec: str | None, target: Target) -> ResolvedQna:
        if counter is not None:
            counter.append(spec)
        version = (mapping or {}).get(spec, "11.0.6.137")
        return ResolvedQna(version=version, artifact_path=Path("/cache/fake.deb"))

    return resolve


SSH_TARGETS = [Target(kind="ssh", name=f"host{i}") for i in range(3)]


# --- fan-out ---------------------------------------------------------------


async def test_single_target_single_version_yields_one_result():
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="host0")],
        transport_factory=lambda t: FakeTransport(t.name),
    )

    assert len(results) == 1
    assert results[0].host == "host0"


async def test_targets_times_versions_fanout():
    results = await evaluate_client_relevance(
        "true",
        SSH_TARGETS,
        qna_version=["11.0", "10.0"],
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver({"11.0": "11.0.6.137", "10.0": "10.0.16.50"}),
    )

    assert len(results) == 6
    pairs = {(r.host, r.qna_version) for r in results}
    assert pairs == {
        (host, version)
        for host in ("host0", "host1", "host2")
        for version in ("11.0.6.137", "10.0.16.50")
    }


async def test_no_version_means_one_result_per_target():
    results = await evaluate_client_relevance(
        "true", SSH_TARGETS, transport_factory=lambda t: FakeTransport(t.name)
    )

    assert len(results) == 3
    assert all(r.qna_version is None for r in results)


async def test_per_target_version_overrides_the_default():
    targets = [
        Target(kind="ssh", name="pinned", qna_version="9.5"),
        Target(kind="ssh", name="default"),
    ]

    results = await evaluate_client_relevance(
        "true",
        targets,
        qna_version="11.0",
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver({"11.0": "11.0.6.137", "9.5": "9.5.22.10"}),
    )

    by_host = {r.host: r.qna_version for r in results}
    assert by_host == {"pinned": "9.5.22.10", "default": "11.0.6.137"}


# --- concurrency -----------------------------------------------------------


async def test_max_parallel_bounds_concurrency():
    tracker = {"live": 0, "peak": 0}
    targets = [Target(kind="ssh", name=f"h{i}") for i in range(8)]

    await evaluate_client_relevance(
        "true",
        targets,
        max_parallel=2,
        transport_factory=lambda t: FakeTransport(t.name, tracker=tracker, delay=0.02),
    )

    assert tracker["peak"] <= 2, f"peak concurrency was {tracker['peak']}"


async def test_work_actually_runs_concurrently():
    tracker = {"live": 0, "peak": 0}
    targets = [Target(kind="ssh", name=f"h{i}") for i in range(4)]

    await evaluate_client_relevance(
        "true",
        targets,
        max_parallel=4,
        transport_factory=lambda t: FakeTransport(t.name, tracker=tracker, delay=0.05),
    )

    assert tracker["peak"] > 1, "fan-out should not be serialized"


# --- version resolution ----------------------------------------------------


async def test_version_resolved_once_per_run_not_once_per_target():
    asked: list[str | None] = []

    await evaluate_client_relevance(
        "true",
        SSH_TARGETS,
        qna_version="11.0",
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver(counter=asked),
    )

    assert asked == ["11.0"], "a spec should be resolved once and shared"


async def test_transports_only_ever_see_resolved_versions():
    made: list[FakeTransport] = []

    def factory(target):
        transport = FakeTransport(target.name)
        made.append(transport)
        return transport

    await evaluate_client_relevance(
        "true",
        SSH_TARGETS,
        qna_version="11.0",
        transport_factory=factory,
        resolver=make_resolver(),
    )

    for transport in made:
        for call in transport.calls:
            assert isinstance(call, ResolvedQna)
            assert call.version == "11.0.6.137", "specs must not reach a transport"


async def test_resolution_failure_becomes_resolve_results():
    from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError

    async def failing_resolver(spec, target):
        raise ResolveError(f"no such stream {spec}")

    results = await evaluate_client_relevance(
        "true",
        SSH_TARGETS,
        qna_version="7.1",
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=failing_resolver,
    )

    assert len(results) == 3
    assert all(r.error_kind == ERROR_KIND_RESOLVE for r in results)
    assert all("7.1" in (r.error or "") for r in results)


async def test_one_version_failing_does_not_block_the_other():
    from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError

    async def picky_resolver(spec, target):
        if spec == "7.1":
            raise ResolveError("no such stream 7.1")
        return ResolvedQna(version="11.0.6.137", artifact_path=Path("/cache/fake.deb"))

    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="host0")],
        qna_version=["11.0", "7.1"],
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=picky_resolver,
    )

    kinds = sorted((r.error_kind or "ok") for r in results)
    assert kinds == ["ok", ERROR_KIND_RESOLVE]


# --- container platform probing ---------------------------------------------
#
# Issue #1: the resolver picks the artifact (deb vs rpm) from target.platform,
# so a container with no declared platform must be probed BEFORE resolution —
# probing in the transport alone would still download the wrong agent.


class FakeProbingTransport(FakeTransport):
    """A container-style transport whose probe answers with a canned platform."""

    def __init__(self, host: str, platform_key: str | Exception = "rhel", **kwargs) -> None:
        super().__init__(host, **kwargs)
        self.platform_key = platform_key
        self.probed = 0

    async def resolve_platform(self, *, timeout_s: float = 30.0) -> str:
        self.probed += 1
        if isinstance(self.platform_key, Exception):
            raise self.platform_key
        return self.platform_key


def platform_recording_resolver(seen: list[str | None]):
    async def resolve(spec: str | None, target: Target) -> ResolvedQna:
        seen.append(target.platform)
        return ResolvedQna(version="11.0.6.137", artifact_path=Path("/cache/fake.rpm"))

    return resolve


async def test_container_platform_is_probed_before_resolution():
    seen: list[str | None] = []

    await evaluate_client_relevance(
        "true",
        [Target(kind="container", name="almalinux:9", image="almalinux:9")],
        qna_version="11.0",
        transport_factory=lambda t: FakeProbingTransport(t.name, "rhel"),
        resolver=platform_recording_resolver(seen),
    )

    assert seen == ["rhel"], "the resolver must see the probed platform, not None"


async def test_probed_platforms_split_the_resolution_dedupe():
    seen: list[str | None] = []
    platforms = {"almalinux:9": "rhel", "ubuntu:22.04": "ubuntu"}

    await evaluate_client_relevance(
        "true",
        [
            Target(kind="container", name=image, image=image)
            for image in ("almalinux:9", "ubuntu:22.04")
        ],
        qna_version="11.0",
        transport_factory=lambda t: FakeProbingTransport(t.name, platforms[t.name]),
        resolver=platform_recording_resolver(seen),
    )

    assert sorted(seen, key=str) == ["rhel", "ubuntu"], (
        "different platforms need distinct artifacts"
    )


async def test_matching_probed_platforms_share_one_resolution():
    seen: list[str | None] = []

    await evaluate_client_relevance(
        "true",
        [
            Target(kind="container", name=image, image=image)
            for image in ("almalinux:9", "rockylinux:9")
        ],
        qna_version="11.0",
        transport_factory=lambda t: FakeProbingTransport(t.name, "rhel"),
        resolver=platform_recording_resolver(seen),
    )

    assert seen == ["rhel"], "same probed platform must share one download"


async def test_probe_failure_becomes_a_bootstrap_result():
    from bigfix_remote_client_relevance.bootstrap.targets import UnknownTargetError

    results = await evaluate_client_relevance(
        "true",
        [Target(kind="container", name="weird:latest", image="weird:latest")],
        qna_version="11.0",
        transport_factory=lambda t: FakeProbingTransport(
            t.name, UnknownTargetError("cannot classify; pass --platform")
        ),
        resolver=make_resolver(),
    )

    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_BOOTSTRAP
    assert "platform" in (results[0].error or "").lower()


async def test_local_platform_is_probed_before_resolution_too(monkeypatch):
    """TransportLocal.resolve_platform is the same fast, no-round-trip probe
    as the other transports' -- it must fill result.platform the same way,
    or a qna_version fan-out leaves every local result indistinguishable
    (render.label needs this to tell them apart)."""
    import sys

    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    monkeypatch.setattr(sys, "platform", "darwin")
    seen: list[str | None] = []

    results = await evaluate_client_relevance(
        "true",
        [Target(kind="local", name="local")],
        qna_version="11.0",
        transport_factory=default_transport_factory,
        resolver=platform_recording_resolver(seen),
    )

    assert seen == ["macos"], "the resolver must see the probed platform, not None"
    assert results[0].platform == "macos"


async def test_probe_engine_failure_becomes_a_transport_result():
    from bigfix_remote_client_relevance.transports.container import ContainerEngineError

    results = await evaluate_client_relevance(
        "true",
        [Target(kind="container", name="weird:latest", image="weird:latest")],
        qna_version="11.0",
        transport_factory=lambda t: FakeProbingTransport(
            t.name, ContainerEngineError("cannot connect to the Docker daemon")
        ),
        resolver=make_resolver(),
    )

    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_TRANSPORT


# --- result.platform and corrective reprobing --------------------------------
#
# An explicit platform is trusted outright (never probed), which is exactly
# the problem when it's wrong: the resolver picks the wrong artifact, it
# fails to extract, and nothing else ever re-checks -- it fails identically
# forever. A bootstrap failure with an explicit platform set is cheap to
# double-check via a corrective reprobe, so hosts.toml can be told what's
# actually wrong instead of just failing the same way on every future run.


class FakeReprobingTransport(FakeTransport):
    """Fails with a bootstrap error; exposes a corrective reprobe."""

    def __init__(self, host: str, *, reprobe_to="windows", reprobe_error=None, **kwargs) -> None:
        super().__init__(host, **kwargs)
        self._reprobe_to = reprobe_to
        self._reprobe_error = reprobe_error
        self.reprobed = 0

    async def evaluate_client_relevance(
        self, client_relevance, *, qna_path=None, qna=None, timeout_s=30.0
    ):
        return ClientRelevanceResult(
            host=self.host,
            transport="fake",
            client_relevance=client_relevance,
            error="could not extract qna",
            error_kind=ERROR_KIND_BOOTSTRAP,
        )

    async def reprobe_platform(self, *, timeout_s: float = 30.0) -> str:
        self.reprobed += 1
        if self._reprobe_error is not None:
            raise self._reprobe_error
        return self._reprobe_to


async def test_successful_probe_populates_result_platform():
    """The fill-in half: a freshly probed platform reaches the result too."""
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="container", name="almalinux:9", image="almalinux:9")],
        qna_version="11.0",
        transport_factory=lambda t: FakeProbingTransport(t.name, "rhel"),
        resolver=make_resolver(),
    )

    assert results[0].platform == "rhel"


async def test_explicit_platform_populates_result_platform_on_success():
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="host", platform="ubuntu")],
        qna_version="11.0",
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver(),
    )

    assert results[0].platform == "ubuntu"


async def test_wrong_explicit_platform_is_corrected_after_bootstrap_failure():
    made: list[FakeReprobingTransport] = []

    def factory(t):
        transport = FakeReprobingTransport(t.name, reprobe_to="windows")
        made.append(transport)
        return transport

    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="win-box", platform="ubuntu")],
        qna_version="11.0",
        transport_factory=factory,
        resolver=make_resolver(),
    )

    assert results[0].platform == "windows"
    assert made[0].reprobed == 1


async def test_correctly_configured_platform_is_left_alone_after_failure():
    """A bootstrap failure for an unrelated reason must not report a false mismatch."""
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="deb-box", platform="ubuntu")],
        qna_version="11.0",
        transport_factory=lambda t: FakeReprobingTransport(t.name, reprobe_to="ubuntu"),
        resolver=make_resolver(),
    )

    assert results[0].platform == "ubuntu"


async def test_reprobe_is_never_attempted_when_platform_was_not_explicit():
    """A freshly probed platform is already trusted; nothing to double-check."""
    made: list[FakeReprobingTransport] = []

    def factory(t):
        transport = FakeReprobingTransport(t.name, reprobe_to="windows")
        made.append(transport)
        return transport

    await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="unknown-box")],  # platform left unset
        qna_version="11.0",
        transport_factory=factory,
        resolver=make_resolver(),
    )

    assert made[0].reprobed == 0


async def test_reprobe_is_skipped_for_a_non_bootstrap_failure():
    class FakeRelevanceFailingTransport(FakeReprobingTransport):
        async def evaluate_client_relevance(
            self, client_relevance, *, qna_path=None, qna=None, timeout_s=30.0
        ):
            return ClientRelevanceResult(
                host=self.host,
                transport="fake",
                client_relevance=client_relevance,
                error="bad inspector",
                error_kind=ERROR_KIND_RELEVANCE,
            )

    made: list[FakeRelevanceFailingTransport] = []

    def factory(t):
        transport = FakeRelevanceFailingTransport(t.name)
        made.append(transport)
        return transport

    await evaluate_client_relevance(
        "namez of it",
        [Target(kind="ssh", name="host", platform="ubuntu")],
        qna_version="11.0",
        transport_factory=factory,
        resolver=make_resolver(),
    )

    assert made[0].reprobed == 0


async def test_a_failed_reprobe_never_masks_the_original_error():
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="host", platform="ubuntu")],
        qna_version="11.0",
        transport_factory=lambda t: FakeReprobingTransport(
            t.name, reprobe_error=RuntimeError("connection dropped")
        ),
        resolver=make_resolver(),
    )

    assert results[0].error_kind == ERROR_KIND_BOOTSTRAP
    assert results[0].platform == "ubuntu", "unchanged: the reprobe attempt itself failed"


def test_default_factory_no_longer_defaults_containers_to_ubuntu():
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    transport = default_transport_factory(
        Target(kind="container", name="almalinux:9", image="almalinux:9")
    )

    assert transport._target is None, "an unset platform must be probed, never assumed"


async def test_explicit_platform_skips_the_orchestrator_probe():
    seen: list[str | None] = []
    made: list[FakeProbingTransport] = []

    def factory(target):
        transport = FakeProbingTransport(target.name, "ubuntu")
        made.append(transport)
        return transport

    await evaluate_client_relevance(
        "true",
        [Target(kind="container", name="alma", image="almalinux:9", platform="rhel")],
        qna_version="11.0",
        transport_factory=factory,
        resolver=platform_recording_resolver(seen),
    )

    assert seen == ["rhel"]
    assert made[0].probed == 0


async def test_no_probe_when_no_version_is_pinned():
    """Without provisioning the platform never selects an artifact; skip the probe."""
    made: list[FakeProbingTransport] = []

    def factory(target):
        transport = FakeProbingTransport(target.name, "rhel")
        made.append(transport)
        return transport

    await evaluate_client_relevance(
        "true",
        [Target(kind="container", name="almalinux:9", image="almalinux:9")],
        transport_factory=factory,
    )

    assert made[0].probed == 0


def test_rebuild_image_reaches_the_transport_factory():
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    target = Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04", rebuild_image=True)
    transport = default_transport_factory(target)

    assert transport._rebuild_image is True


def test_engine_docker_constructs_a_docker_engine():
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    target = Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04", engine="docker")
    transport = default_transport_factory(target)

    assert isinstance(transport._engine, DockerEngine)


def test_engine_podman_constructs_a_podman_engine():
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory
    from bigfix_remote_client_relevance.transports.container import PodmanEngine

    target = Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04", engine="podman")
    transport = default_transport_factory(target)

    assert isinstance(transport._engine, PodmanEngine)


def test_engine_auto_is_the_default():
    from bigfix_remote_client_relevance.orchestrate import Target

    assert Target(kind="container", name="x").engine == "auto"


def test_engine_auto_prefers_docker_when_it_answers(monkeypatch):
    """auto must be a no-op for anyone not using podman: docker, if it works, wins."""
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    monkeypatch.setattr(DockerEngine, "_get_client", lambda self: object())

    target = Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04", engine="auto")
    transport = default_transport_factory(target)

    assert isinstance(transport._engine, DockerEngine)


def test_engine_auto_falls_back_to_podman_when_docker_is_unreachable(monkeypatch):
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory
    from bigfix_remote_client_relevance.transports.container import (
        ContainerEngineError,
        DockerEngine,
        PodmanEngine,
    )

    def _no_docker(self):
        raise ContainerEngineError("cannot connect to the Docker daemon")

    monkeypatch.setattr(DockerEngine, "_get_client", _no_docker)

    target = Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04", engine="auto")
    transport = default_transport_factory(target)

    assert isinstance(transport._engine, PodmanEngine)


def test_become_reaches_the_local_transport():
    """`become` was plumbed for ssh only; local dropped it on the floor."""
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    transport = default_transport_factory(Target(kind="local", name="local", become=True))

    assert transport._become is True


def test_unspecified_local_become_implies_true_on_macos(monkeypatch):
    """qna needs root on macOS unconditionally -- an inventory host or a CLI
    invocation that never mentions `become` should still get it there."""
    import sys

    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    monkeypatch.setattr(sys, "platform", "darwin")

    transport = default_transport_factory(Target(kind="local", name="local", become=None))

    assert transport._become is True


def test_unspecified_local_become_is_false_off_macos(monkeypatch):
    import sys

    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    monkeypatch.setattr(sys, "platform", "linux")

    transport = default_transport_factory(Target(kind="local", name="local", become=None))

    assert transport._become is False


def test_explicit_false_overrides_the_macos_default(monkeypatch):
    """The implied default must still be escapable, e.g. from an inventory file."""
    import sys

    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    monkeypatch.setattr(sys, "platform", "darwin")

    transport = default_transport_factory(Target(kind="local", name="local", become=False))

    assert transport._become is False


def test_unspecified_ssh_become_is_false_even_on_macos(monkeypatch):
    """SSH gets no platform-aware default: the remote OS isn't known up front."""
    import sys

    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    monkeypatch.setattr(sys, "platform", "darwin")

    transport = default_transport_factory(
        Target(kind="ssh", name="test-host", become=None, platform="ubuntu")
    )

    assert transport._become is False


# --- two budgets: image work and evaluation ------------------------------------
#
# One semaphore throttled everything, but pulls and evaluations have very
# different cost profiles: eight simultaneous multi-hundred-MB pulls will
# saturate a laptop while eight evaluations barely register.


class FakePreparingTransport(FakeProbingTransport):
    """A container-style transport whose image phase is separately observable."""

    def __init__(
        self, host, *, image_tracker=None, prepare_delay=0.0, fail_prepare=False, **kw
    ) -> None:
        super().__init__(host, **kw)
        self._image_tracker = image_tracker if image_tracker is not None else {}
        self._image_tracker.setdefault("live", 0)
        self._image_tracker.setdefault("peak", 0)
        self._image_tracker.setdefault("calls", 0)
        self._prepare_delay = prepare_delay
        self._fail_prepare = fail_prepare
        self.order: list[str] = []

    async def prepare(self, *, qna=None, timeout_s=30.0) -> None:
        self.order.append("prepare")
        self._image_tracker["calls"] += 1
        self._image_tracker["live"] += 1
        self._image_tracker["peak"] = max(self._image_tracker["peak"], self._image_tracker["live"])
        try:
            if self._prepare_delay:
                await asyncio.sleep(self._prepare_delay)
            if self._fail_prepare:
                raise RuntimeError("could not pull the image")
        finally:
            self._image_tracker["live"] -= 1

    async def evaluate_client_relevance(self, *args, **kwargs):
        self.order.append("evaluate")
        return await super().evaluate_client_relevance(*args, **kwargs)


def container_targets(count: int) -> list[Target]:
    return [Target(kind="container", name=f"image{i}", image=f"image{i}") for i in range(count)]


async def test_the_image_phase_runs_before_the_evaluation():
    made: list[FakePreparingTransport] = []

    def factory(target):
        transport = FakePreparingTransport(target.name)
        made.append(transport)
        return transport

    await evaluate_client_relevance("true", container_targets(1), transport_factory=factory)

    assert made[0].order == ["prepare", "evaluate"]


async def test_pull_parallel_bounds_the_image_phase():
    tracker = {"live": 0, "peak": 0, "calls": 0}

    await evaluate_client_relevance(
        "true",
        container_targets(8),
        max_parallel=8,
        pull_parallel=2,
        transport_factory=lambda t: FakePreparingTransport(
            t.name, image_tracker=tracker, prepare_delay=0.02
        ),
    )

    assert tracker["calls"] == 8, "every target must have gone through the image phase"
    assert tracker["peak"] <= 2, f"peak image concurrency was {tracker['peak']}"


async def test_the_two_budgets_are_independent():
    """A low pull limit must not throttle evaluation, or vice versa.

    The image phase is deliberately much faster than the evaluation here, so
    targets finish preparing and pile up in the evaluation phase — which is
    where they must be allowed to overlap.
    """
    images = {"live": 0, "peak": 0, "calls": 0}
    evals = {"live": 0, "peak": 0}

    await evaluate_client_relevance(
        "true",
        container_targets(6),
        max_parallel=6,
        pull_parallel=1,
        transport_factory=lambda t: FakePreparingTransport(
            t.name, image_tracker=images, tracker=evals, prepare_delay=0.005, delay=0.08
        ),
    )

    assert images["calls"] == 6
    assert images["peak"] == 1, "the image phase is serialized by pull_parallel=1"
    assert evals["peak"] > 1, "but evaluation is not"


async def test_a_failed_image_phase_does_not_fail_the_pair():
    """The evaluation reports it in its own vocabulary; prepare is an optimization."""
    made: list[FakePreparingTransport] = []

    def factory(target):
        transport = FakePreparingTransport(target.name, fail_prepare=True)
        made.append(transport)
        return transport

    results = await evaluate_client_relevance(
        "true", container_targets(1), transport_factory=factory
    )

    assert made[0].order[:1] == ["prepare"], "the hook must actually have been tried"
    assert len(results) == 1
    assert results[0].error_kind is None


async def test_transports_without_the_hook_are_not_throttled_by_the_pull_limit():
    """A 20-host SSH sweep must not be serialized by a container-pull budget."""
    tracker = {"live": 0, "peak": 0}

    await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name=f"h{i}") for i in range(8)],
        max_parallel=8,
        pull_parallel=1,
        transport_factory=lambda t: FakeTransport(t.name, tracker=tracker, delay=0.02),
    )

    assert tracker["peak"] > 1


def test_the_default_factory_accepts_a_coordinator():
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory
    from bigfix_remote_client_relevance.transports.coordination import ImageCoordinator

    shared = ImageCoordinator()
    transport = default_transport_factory(
        Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04"), coordinator=shared
    )

    assert transport._coordinator is shared


def test_the_default_factory_still_takes_one_argument():
    """It is a documented public alias; adding a parameter must not break callers."""
    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    for target in (
        Target(kind="local", name="local"),
        Target(kind="ssh", name="host"),
        Target(kind="container", name="ubuntu:22.04", image="ubuntu:22.04"),
    ):
        assert default_transport_factory(target) is not None


async def test_results_still_follow_targets_then_versions():
    """The new phases must not disturb the documented result ordering."""
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="ssh", name="h0"), Target(kind="ssh", name="h1")],
        qna_version=["11.0", "9.5"],
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver({"11.0": "11.0.6.137", "9.5": "9.5.22.10"}),
    )

    assert [(r.host, r.qna_version) for r in results] == [
        ("h0", "11.0.6.137"),
        ("h0", "9.5.22.10"),
        ("h1", "11.0.6.137"),
        ("h1", "9.5.22.10"),
    ]


# --- streaming -------------------------------------------------------------


async def test_stream_yields_a_result_before_the_slow_target_finishes():
    """The whole point: a fast host is not held hostage by a slow one."""
    slow = Target(kind="ssh", name="slow")
    fast = Target(kind="ssh", name="fast")

    def factory(target):
        return FakeTransport(target.name, delay=0.20 if target.name == "slow" else 0.0)

    first = None
    async for result in evaluate_client_relevance_stream(
        "true", [slow, fast], transport_factory=factory
    ):
        first = result
        break

    assert first is not None
    assert first.host == "fast", "results must arrive in completion order, not target order"


async def test_breaking_out_of_the_stream_cancels_the_rest():
    """An abandoned stream must not leave evaluations running against live
    connections with nobody to collect them."""
    started: list[str] = []
    finished: list[str] = []

    class TrackingTransport(FakeTransport):
        async def evaluate_client_relevance(self, client_relevance, **kwargs):
            started.append(self.host)
            result = await super().evaluate_client_relevance(client_relevance, **kwargs)
            finished.append(self.host)
            return result

    def factory(target):
        return TrackingTransport(target.name, delay=0.0 if target.name == "fast" else 5.0)

    stream = evaluate_client_relevance_stream(
        "true",
        [Target(kind="ssh", name="fast"), Target(kind="ssh", name="slow")],
        transport_factory=factory,
    )
    async for _result in stream:
        break
    await stream.aclose()

    assert finished == ["fast"], "the slow evaluation should have been cancelled, not awaited"


async def test_stream_yields_every_pair_exactly_once():
    seen = []
    async for result in evaluate_client_relevance_stream(
        "true",
        [Target(kind="ssh", name="h0"), Target(kind="ssh", name="h1")],
        qna_version=["11.0", "9.5"],
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver({"11.0": "11.0.6.137", "9.5": "9.5.22.10"}),
    ):
        seen.append((result.host, result.qna_version))

    assert sorted(seen) == [
        ("h0", "11.0.6.137"),
        ("h0", "9.5.22.10"),
        ("h1", "11.0.6.137"),
        ("h1", "9.5.22.10"),
    ]


async def test_stream_reports_a_failing_target_as_a_result_not_a_raise():
    """Streaming keeps the orchestrator's contract: failures are results."""

    class ExplodingTransport(FakeTransport):
        async def evaluate_client_relevance(self, *a, **k):
            raise RuntimeError("host is on fire")

    def factory(target):
        return (
            ExplodingTransport(target.name) if target.name == "bad" else FakeTransport(target.name)
        )

    seen = {}
    async for result in evaluate_client_relevance_stream(
        "true",
        [Target(kind="ssh", name="bad"), Target(kind="ssh", name="good")],
        transport_factory=factory,
    ):
        seen[result.host] = result

    assert seen["bad"].error_kind == ERROR_KIND_TRANSPORT
    assert seen["good"].error_kind is None


async def test_stream_and_batch_agree_on_the_same_run():
    """Two entry points, one machinery -- they must not drift apart."""
    targets = [Target(kind="ssh", name=f"h{i}") for i in range(3)]

    def factory(target):
        return FakeTransport(target.name)

    batched = await evaluate_client_relevance("true", targets, transport_factory=factory)
    streamed = [
        r
        async for r in evaluate_client_relevance_stream("true", targets, transport_factory=factory)
    ]

    assert sorted(r.host for r in batched) == sorted(r.host for r in streamed)


def test_count_work_matches_what_the_fanout_produces():
    """The CLI predicts the result count to format its first streamed result,
    so this must stay in step with the grid the orchestrator actually builds."""
    targets = [
        Target(kind="ssh", name="h0"),
        Target(kind="ssh", name="h1", qna_version=["11.0", "9.5"]),
    ]

    assert count_work(targets, "11.0") == 3


# --- failure isolation -----------------------------------------------------


async def test_one_target_failing_does_not_cancel_the_others():
    class ExplodingTransport(FakeTransport):
        async def evaluate_client_relevance(self, *a, **k):
            raise RuntimeError("host is on fire")

    def factory(target):
        return (
            ExplodingTransport(target.name)
            if target.name == "host1"
            else FakeTransport(target.name)
        )

    results = await evaluate_client_relevance("true", SSH_TARGETS, transport_factory=factory)

    assert len(results) == 3
    by_host = {r.host: r.error_kind for r in results}
    assert by_host["host1"] == ERROR_KIND_TRANSPORT
    assert by_host["host0"] is None
    assert by_host["host2"] is None


async def test_fastquery_with_a_version_fails_at_resolution():
    """Fast Query evaluates with whatever agent is installed; pinning is an error."""
    results = await evaluate_client_relevance(
        "true",
        [Target(kind="fastquery", name="deployment")],
        qna_version="11.0",
        transport_factory=lambda t: FakeTransport(t.name),
        resolver=make_resolver(),
    )

    assert len(results) == 1
    assert results[0].error_kind in {ERROR_KIND_RESOLVE, ERROR_KIND_BOOTSTRAP}
    assert "version" in (results[0].error or "").lower()


# --- exit codes ------------------------------------------------------------


def result_with(kind: str | None, answers: list[str] | None = None) -> ClientRelevanceResult:
    return ClientRelevanceResult(
        host="h",
        transport="fake",
        client_relevance="true",
        answers=answers if answers is not None else ["yes"],
        error_kind=kind,
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (None, EXIT_OK),
        (ERROR_KIND_RELEVANCE, EXIT_RELEVANCE),
        (ERROR_KIND_QNA, EXIT_QNA),
        (ERROR_KIND_BOOTSTRAP, EXIT_QNA),
        (ERROR_KIND_TRANSPORT, EXIT_TRANSPORT),
        (ERROR_KIND_RESOLVE, EXIT_RESOLVE),
    ],
)
def test_exit_code_per_error_kind(kind, expected):
    assert worst_exit_code([result_with(kind)]) == expected


def test_worst_code_wins_across_the_fanout():
    results = [
        result_with(None),
        result_with(ERROR_KIND_RELEVANCE),
        result_with(ERROR_KIND_RESOLVE),
        result_with(ERROR_KIND_QNA),
    ]

    assert worst_exit_code(results) == EXIT_RESOLVE


def test_empty_answer_set_without_an_error_is_success():
    """A plural inspector matching nothing is a valid answer, not a failure.

    DESIGN.md said exit 0 required at least one answer; that would fail CI on a
    legitimately empty result, so success is defined by the absence of an error.
    """
    assert worst_exit_code([result_with(None, answers=[])]) == EXIT_OK


def test_no_results_is_not_a_success():
    assert worst_exit_code([]) != EXIT_OK


# --- cancellation -----------------------------------------------------------
#
# An MCP server cancels in-flight requests as a matter of course, which lands
# inside a fan-out as CancelledError. _one wraps every stage in a broad
# `except Exception`, so these pin that cancellation escapes it rather than
# being turned into a result with error_kind="transport".


class HangingTransport:
    """Blocks forever, and records whether it was cancelled."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = False

    async def evaluate_client_relevance(
        self, client_relevance, *, qna_path=None, qna=None, timeout_s=30.0
    ):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


async def test_cancelling_the_fanout_is_not_swallowed_into_a_result():
    transport = HangingTransport()
    task = asyncio.create_task(
        evaluate_client_relevance(
            "true",
            [Target(kind="local", name="local")],
            transport_factory=lambda target: transport,
        )
    )
    await asyncio.wait_for(transport.entered.wait(), timeout=5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancellation_reaches_the_transport():
    """Not just the caller: the in-flight transport call is cancelled too, so a
    transport with cleanup of its own gets the chance to run it."""
    transport = HangingTransport()
    task = asyncio.create_task(
        evaluate_client_relevance(
            "true",
            [Target(kind="local", name="local")],
            transport_factory=lambda target: transport,
        )
    )
    await asyncio.wait_for(transport.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert transport.cancelled


async def test_cancelling_the_stream_is_not_swallowed_into_a_result():
    transport = HangingTransport()

    async def consume():
        async for _ in evaluate_client_relevance_stream(
            "true",
            [Target(kind="local", name="local")],
            transport_factory=lambda target: transport,
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(transport.entered.wait(), timeout=5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
