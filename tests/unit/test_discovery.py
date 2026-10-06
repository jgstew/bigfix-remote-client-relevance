"""Tests for first-run inventory auto-discovery.

The evaluator is injected throughout; no transport is ever constructed here.
"""

from __future__ import annotations

import tomllib

from bigfix_remote_client_relevance.discovery import (
    candidate_groups,
    discover,
    write_discovered,
)
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
)


def _ok(host: str) -> ClientRelevanceResult:
    return ClientRelevanceResult(
        host=host,
        transport="fake",
        client_relevance="x",
        answers=["Linux"],
        answer_types=["string"],
    )


def _failed(host: str) -> ClientRelevanceResult:
    return ClientRelevanceResult(
        host=host,
        transport="fake",
        client_relevance="x",
        error="boom",
        error_kind=ERROR_KIND_TRANSPORT,
    )


def test_candidates_without_engine_are_local_and_web_eval():
    groups = candidate_groups(engine_available=False, arch="x86_64")
    hosts = {name: entry for group in groups for name, entry in group}

    assert [[name for name, _ in group] for group in groups] == [["local"], ["web-eval-rhel"]]
    assert hosts["local"] == {"transport": "local", "qna_version": []}
    assert hosts["web-eval-rhel"] == {
        "transport": "online_evaluator",
        "base_url": "https://developer.bigfix.com",
        "qna_version": [],
        "arch": "x86_64",
    }


def test_candidates_with_engine_add_debian_and_rhel_family_fallback_chains():
    groups = candidate_groups(engine_available=True, arch="arm64")
    hosts = {name: entry for group in groups for name, entry in group}

    assert [[name for name, _ in group] for group in groups] == [
        ["local"],
        ["web-eval-rhel"],
        ["ubuntu-26-04", "ubuntu-24-04"],
        ["ubi10", "ubi9"],
    ]
    assert hosts["ubuntu-24-04"]["image"] == "ubuntu:24.04"
    assert hosts["ubi9"]["image"] == "registry.access.redhat.com/ubi9/ubi"

    assert hosts["ubuntu-26-04"] == {
        "transport": "container",
        "image": "ubuntu:26.04",
        "arch": "arm64",
        "qna_version": "11.0",
    }
    assert hosts["ubi10"] == {
        "transport": "container",
        "image": "registry.access.redhat.com/ubi10/ubi",
        "arch": "arm64",
        "qna_version": "11.0",
    }


async def test_discover_keeps_only_hosts_that_evaluated_cleanly():
    seen: list[str] = []

    async def fake_evaluate(client_relevance, targets, **kwargs):
        seen.extend(t.name for t in targets)
        return [_ok(t.name) if t.name != "local" else _failed(t.name) for t in targets]

    found = await discover(engine_available=False, arch="x86_64", evaluate=fake_evaluate)

    assert set(seen) == {"local", "web-eval-rhel"}
    assert set(found) == {"web-eval-rhel"}


async def test_discover_rejects_host_with_any_failed_result():
    async def fake_evaluate(client_relevance, targets, **kwargs):
        (target,) = targets
        if target.name == "local":
            return [_ok("local"), _failed("local")]
        return [_ok(target.name)]

    found = await discover(engine_available=False, arch="x86_64", evaluate=fake_evaluate)

    assert set(found) == {"web-eval-rhel"}


async def test_discover_skips_names_already_known():
    seen: list[str] = []

    async def fake_evaluate(client_relevance, targets, **kwargs):
        seen.extend(t.name for t in targets)
        return [_ok(t.name) for t in targets]

    found = await discover(
        engine_available=False, arch="x86_64", evaluate=fake_evaluate, skip={"local"}
    )

    assert seen == ["web-eval-rhel"]
    assert set(found) == {"web-eval-rhel"}


def test_write_discovered_creates_file_and_parent(tmp_path):
    path = tmp_path / ".bigfix" / "remote_clients.toml"

    added = write_discovered(path, {"local": {"transport": "local", "qna_version": []}})

    assert added == ["local"]
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    assert document["hosts"]["local"] == {"transport": "local", "qna_version": []}


def test_write_discovered_with_nothing_does_not_create_file(tmp_path):
    path = tmp_path / ".bigfix" / "remote_clients.toml"

    assert write_discovered(path, {}) == []
    assert not path.exists()


def test_write_discovered_merges_without_overwriting_or_losing_comments(tmp_path):
    path = tmp_path / "remote_clients.toml"
    path.write_text(
        '# keep me\n[hosts.local]\ntransport = "local"\nbecome = false\n', encoding="utf-8"
    )

    added = write_discovered(
        path,
        {
            "local": {"transport": "local", "qna_version": []},
            "ubi10": {"transport": "container", "image": "x"},
        },
    )

    assert added == ["ubi10"]
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    document = tomllib.loads(text)
    assert document["hosts"]["local"] == {"transport": "local", "become": False}
    assert document["hosts"]["ubi10"]["image"] == "x"


async def test_discover_matches_results_by_target_not_by_result_host_label():
    """Container results label host as e.g. container:ubuntu:26.04@arm64, not the inventory name."""

    async def fake_evaluate(client_relevance, targets, **kwargs):
        return [
            _ok(f"container:{t.image}@{t.arch}" if t.kind == "container" else t.name)
            for t in targets
        ]

    found = await discover(engine_available=True, arch="arm64", evaluate=fake_evaluate)

    assert set(found) == {"local", "web-eval-rhel", "ubuntu-26-04", "ubi10"}


async def test_discover_rejects_target_with_no_results():
    async def fake_evaluate(client_relevance, targets, **kwargs):
        return []

    assert await discover(engine_available=False, arch="x86_64", evaluate=fake_evaluate) == {}


def _container_fake(failing: set[str], seen: list[str]):
    async def fake_evaluate(client_relevance, targets, **kwargs):
        (target,) = targets
        seen.append(target.name)
        return [_failed(target.name) if target.name in failing else _ok(target.name)]

    return fake_evaluate


async def test_family_falls_back_to_next_image_when_newest_fails():
    seen: list[str] = []

    found = await discover(
        engine_available=True,
        arch="arm64",
        evaluate=_container_fake({"ubuntu-26-04", "ubi10"}, seen),
    )

    assert set(found) == {"local", "web-eval-rhel", "ubuntu-24-04", "ubi9"}
    assert seen.index("ubuntu-26-04") < seen.index("ubuntu-24-04")


async def test_family_stops_at_first_image_that_works():
    seen: list[str] = []

    found = await discover(
        engine_available=True, arch="arm64", evaluate=_container_fake(set(), seen)
    )

    assert set(found) == {"local", "web-eval-rhel", "ubuntu-26-04", "ubi10"}
    assert "ubuntu-24-04" not in seen
    assert "ubi9" not in seen


async def test_family_with_every_image_failing_adds_nothing_for_it():
    found = await discover(
        engine_available=True,
        arch="arm64",
        evaluate=_container_fake({"ubuntu-26-04", "ubuntu-24-04"}, []),
    )

    assert "ubuntu-26-04" not in found
    assert "ubuntu-24-04" not in found
    assert "ubi10" in found
