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
    evaluate_client_relevance,
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
        "true", SSH_TARGETS, qna_version="11.0", transport_factory=factory,
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

    def __init__(self, host: str, platform_key="rhel", **kwargs) -> None:
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

    assert sorted(seen, key=str) == ["rhel", "ubuntu"], "different platforms need distinct artifacts"


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

    target = Target(
        kind="container", name="ubuntu:22.04", image="ubuntu:22.04", rebuild_image=True
    )
    transport = default_transport_factory(target)

    assert transport._rebuild_image is True


# --- failure isolation -----------------------------------------------------


async def test_one_target_failing_does_not_cancel_the_others():
    class ExplodingTransport(FakeTransport):
        async def evaluate_client_relevance(self, *a, **k):
            raise RuntimeError("host is on fire")

    def factory(target):
        return ExplodingTransport(target.name) if target.name == "host1" else FakeTransport(
            target.name
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
