"""iOS and Android: what installs there, and what the library does without the rest.

pyproject.toml leaves asyncssh (whose cryptography has no iOS/Android
wheels) and zstandard out on those platforms, and nothing there can start a
process, so qna can't run locally and no container engine exists. These
tests stand in for such an install by blocking the left-out imports and
faking ``sys.platform``; everything stays offline.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest

BLOCKED = ("asyncssh", "cryptography", "docker", "zstandard")
NO_PROCESS = ("ios", "tvos", "watchos", "android", "emscripten", "wasi")
DESKTOP = ("darwin", "linux", "win32")


def _block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the left-out dependencies unimportable, and the package fresh."""
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{name} is blocked to simulate an iOS/Android install")
        return real_import(name, *args, **kwargs)

    for mod in list(sys.modules):
        if mod.split(".")[0] in ("bigfix_remote_client_relevance", *BLOCKED):
            monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setattr(builtins, "__import__", guarded)


def _fail(*args, **kwargs):
    raise AssertionError("must not be reached on a platform that can't start processes")


# --- the package imports ---------------------------------------------------------


def test_package_and_fastquery_import_without_compiled_deps(monkeypatch):
    _block(monkeypatch)
    pkg = importlib.import_module("bigfix_remote_client_relevance")

    assert pkg.TransportFastQuery
    assert pkg.TransportOnlineEvaluator
    assert pkg.evaluate_client_relevance_stream
    for name in BLOCKED:
        assert name not in sys.modules


async def test_fastquery_target_evaluates_without_compiled_deps(monkeypatch):
    """The Fast Query path end to end, over the session library's ClientQuery seam."""
    _block(monkeypatch)
    pkg = importlib.import_module("bigfix_remote_client_relevance")
    from bigfix_remote_session_relevance import ClientQueryResponse, ClientQueryRow

    def response(rows):
        return ClientQueryResponse(
            query_id=7,
            rows=rows,
            error=None,
            connection="lab",
            server="bigfix.example:52311",
            backend="rest",
            elapsed_ms=1.0,
        )

    class FakeBackend:
        """Only the two ClientQuery calls a computer_ids target makes."""

        async def submit_client_query(self, client_relevance, computer_ids, *, use_client_context):
            return response([])

        async def client_query_results(self, query_id):
            return response([ClientQueryRow(1, "phone-target", False, "Linux")])

    async def no_wait(seconds):
        return None

    def factory(target):
        return pkg.TransportFastQuery(FakeBackend(), computer_ids=(1,), sleep=no_wait)

    results = await pkg.evaluate_client_relevance(
        "name of operating system",
        [pkg.Target(kind="fastquery", name="lab", connection="lab", computer_ids=(1,))],
        transport_factory=factory,
    )

    assert [(r.host, r.answers, r.error) for r in results] == [("phone-target", ["Linux"], None)]
    for name in BLOCKED:
        assert name not in sys.modules


# --- ssh without asyncssh ---------------------------------------------------------


async def test_ssh_target_without_asyncssh_is_a_result_not_a_crash(monkeypatch):
    _block(monkeypatch)
    pkg = importlib.import_module("bigfix_remote_client_relevance")

    results = await pkg.evaluate_client_relevance(
        "name of operating system", [pkg.Target(kind="ssh", name="host.example")]
    )

    (result,) = results
    assert result.error_kind == pkg.ERROR_KIND_TRANSPORT
    assert result.host == "host.example"
    assert "asyncssh" in result.error
    assert sys.platform in result.error
    assert "fastquery" in result.error and "online_evaluator" in result.error


async def test_pinned_ssh_target_without_asyncssh_never_resolves_a_version(monkeypatch):
    """A pinned version probes the host first; that fails the same clear way."""
    _block(monkeypatch)
    pkg = importlib.import_module("bigfix_remote_client_relevance")

    results = await pkg.evaluate_client_relevance(
        "name of operating system",
        [pkg.Target(kind="ssh", name="host.example", qna_version="11.0")],
        resolver=_fail,
    )

    (result,) = results
    assert result.error_kind == pkg.ERROR_KIND_TRANSPORT
    assert "asyncssh" in result.error and "fastquery" in result.error


def test_ssh_discovery_without_asyncssh_says_so_and_contacts_nothing(monkeypatch, tmp_path):
    _block(monkeypatch)
    ssh_discovery = importlib.import_module("bigfix_remote_client_relevance.ssh_discovery")
    told: list[str] = []

    added = ssh_discovery.run_ssh_discovery(
        tmp_path / "remote_clients.toml",
        ask=_fail,
        tell=told.append,
        ssh_dir=tmp_path / ".ssh",
        resolve=_fail,
        keyscan=_fail,
        evaluate=_fail,
        browse=_fail,
        lookup=_fail,
        self_keys=set(),
        self_names=set(),
    )

    assert added == []
    assert any("asyncssh" in line and "fastquery" in line for line in told), told
    assert not (tmp_path / "remote_clients.toml").exists()


# --- can_run_processes ------------------------------------------------------------


@pytest.mark.parametrize("platform", NO_PROCESS)
def test_cannot_run_processes_on_mobile_and_wasm(monkeypatch, platform):
    from bigfix_remote_client_relevance import can_run_processes

    monkeypatch.setattr(sys, "platform", platform)
    assert can_run_processes() is False


@pytest.mark.parametrize("platform", DESKTOP)
def test_can_run_processes_on_desktop(monkeypatch, platform):
    from bigfix_remote_client_relevance import can_run_processes

    monkeypatch.setattr(sys, "platform", platform)
    assert can_run_processes() is True


# --- discovery offers nothing that needs a process -------------------------------


def test_base_candidates_on_ios_are_only_the_online_evaluator(monkeypatch):
    from bigfix_remote_client_relevance import discovery

    monkeypatch.setattr(sys, "platform", "ios")
    assert [name for name, _ in discovery._base_candidates()] == ["web-eval-rhel"]


@pytest.mark.parametrize("platform", DESKTOP)
def test_base_candidates_on_desktop_are_unchanged(monkeypatch, platform):
    from bigfix_remote_client_relevance import discovery

    monkeypatch.setattr(sys, "platform", platform)
    assert [name for name, _ in discovery._base_candidates()] == [
        "local",
        "local-downloaded",
        "web-eval-rhel",
    ]


async def test_discover_on_ios_never_looks_for_a_container_engine(monkeypatch):
    from bigfix_remote_client_relevance import discovery

    monkeypatch.setattr(sys, "platform", "ios")
    monkeypatch.setattr(discovery, "default_image_source", _fail)
    seen: list[str] = []

    async def fake_evaluate(client_relevance, targets, **kwargs):
        (target,) = targets
        seen.append(target.name)
        from bigfix_remote_client_relevance.results import ClientRelevanceResult

        return [
            ClientRelevanceResult(
                host=target.name, transport="fake", client_relevance="x", answers=["Linux"]
            )
        ]

    found = await discovery.discover(arch="arm64", evaluate=fake_evaluate)

    assert seen == ["web-eval-rhel"]
    assert set(found) == {"web-eval-rhel"}


@pytest.mark.parametrize("platform", DESKTOP)
async def test_discover_on_desktop_still_looks_for_a_container_engine(monkeypatch, platform):
    from bigfix_remote_client_relevance import discovery

    monkeypatch.setattr(sys, "platform", platform)
    asked: list[bool] = []

    def no_engine() -> None:
        asked.append(True)

    monkeypatch.setattr(discovery, "default_image_source", no_engine)

    async def fake_evaluate(client_relevance, targets, **kwargs):
        return []

    await discovery.discover(arch="arm64", evaluate=fake_evaluate)

    assert asked == [True]


# --- a local target fails before resolving or downloading anything ----------------


@pytest.mark.parametrize("qna_version", ["11.0", []])
async def test_local_target_on_ios_fails_before_resolving_or_downloading(monkeypatch, qna_version):
    from bigfix_remote_client_relevance.bootstrap import cache, release_site
    from bigfix_remote_client_relevance.orchestrate import (
        Target,
        default_transport_factory,
        evaluate_client_relevance,
    )
    from bigfix_remote_client_relevance.results import ERROR_KIND_TRANSPORT

    monkeypatch.setattr(sys, "platform", "ios")
    monkeypatch.setattr(cache, "ensure_artifact", _fail)
    monkeypatch.setattr(release_site, "resolve_version_spec", _fail)
    monkeypatch.setattr(release_site, "artifact_for", _fail)

    results = await evaluate_client_relevance(
        "name of operating system",
        [Target(kind="local", name="local", qna_version=qna_version)],
        transport_factory=default_transport_factory,
    )

    (result,) = results
    assert result.error_kind == ERROR_KIND_TRANSPORT
    error = result.error or ""
    assert "ios" in error
    assert "fastquery" in error and "online_evaluator" in error


async def test_local_target_on_ios_never_calls_an_injected_resolver(monkeypatch):
    from bigfix_remote_client_relevance.orchestrate import Target, evaluate_client_relevance

    monkeypatch.setattr(sys, "platform", "ios")

    results = await evaluate_client_relevance(
        "name of operating system",
        [Target(kind="local", name="local", qna_version="11.0")],
        transport_factory=_fail,
        resolver=_fail,
    )

    assert "ios" in (results[0].error or "")


async def test_local_target_on_desktop_still_resolves(monkeypatch):
    from bigfix_remote_client_relevance.orchestrate import Target, evaluate_client_relevance
    from bigfix_remote_client_relevance.results import ClientRelevanceResult, ResolvedQna

    monkeypatch.setattr(sys, "platform", "linux")
    asked: list[str | None] = []

    async def resolver(spec, target):
        asked.append(spec)
        return ResolvedQna(version="11.0.7.61", artifact_path=Path("/cache/fake.deb"))

    class Fake:
        async def evaluate_client_relevance(self, client_relevance, **kwargs):
            return ClientRelevanceResult(
                host="local", transport="local", client_relevance=client_relevance, answers=["x"]
            )

    results = await evaluate_client_relevance(
        "name of operating system",
        [Target(kind="local", name="local", qna_version="11.0", platform="ubuntu", arch="x86_64")],
        transport_factory=lambda target: Fake(),
        resolver=resolver,
    )

    assert asked == ["11.0"]
    assert results[0].error is None
