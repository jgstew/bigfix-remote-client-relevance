"""Tests for first-run inventory auto-discovery.

The evaluator and the image source are injected throughout; no transport,
container engine or registry is ever touched here.
"""

from __future__ import annotations

import tomllib

import pytest

from bigfix_remote_client_relevance import discovery as discovery_module
from bigfix_remote_client_relevance.discovery import (
    EngineImageSource,
    default_image_source,
    discover,
    host_name_for,
    run_auto_discovery,
    write_discovered,
)
from bigfix_remote_client_relevance.inventory import (
    EmptyInventoryError,
    InventoryError,
    load_inventory,
)
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
)

UBI10 = "registry.access.redhat.com/ubi10/ubi"
UBI9 = "registry.access.redhat.com/ubi9/ubi"


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


class FakeImages:
    """Stands in for the container engine + registry."""

    def __init__(self, local: list[str] | None = None, platforms: dict | None = None) -> None:
        self._local = local or []
        # image -> set of archs; missing key -> None (registry unreachable)
        self._platforms = platforms or {}

    def local_tags(self) -> list[str]:
        return list(self._local)

    def platforms(self, image: str) -> set[str] | None:
        return self._platforms.get(image)


def _evaluator(seen: list, *, failing: set[str] = frozenset(), results=None):
    """One target per call; record names; fail the named ones."""

    async def fake_evaluate(client_relevance, targets, **kwargs):
        (target,) = targets
        seen.append(target.name)
        if results is not None:
            return results(target)
        return [_failed(target.name) if target.name in failing else _ok(target.name)]

    return fake_evaluate


async def _discover(images, seen, *, arch="arm64", failing=frozenset(), skip=(), results=None):
    return await discover(
        images=images,
        arch=arch,
        evaluate=_evaluator(seen, failing=failing, results=results),
        skip=skip,
    )


BASE = {"local", "local-downloaded", "web-eval-rhel"}


# --- naming -------------------------------------------------------------------


def test_host_names():
    assert host_name_for("ubuntu:26.04", "arm64") == "ubuntu-26-04-arm64"
    assert host_name_for(UBI10, "x86_64") == "ubi10-x86_64"
    assert host_name_for("debian:12", "arm64") == "debian-12-arm64"
    assert host_name_for("almalinux:latest", "arm64") == "almalinux-arm64"
    assert host_name_for("docker.io/library/fedora:42", "arm64") == "fedora-42-arm64"


# --- no container engine --------------------------------------------------------


async def test_without_engine_tries_local_pair_and_web_eval():
    seen: list = []
    found = await _discover(None, seen)

    assert set(seen) == BASE
    assert set(found) == BASE
    assert found["local"] == {"transport": "local", "qna_version": []}
    assert found["local-downloaded"] == {"transport": "local", "qna_version": "11.0"}
    assert found["web-eval-rhel"] == {
        "transport": "online_evaluator",
        "base_url": "https://developer.bigfix.com",
        "qna_version": [],
        "arch": "x86_64",
    }


async def test_local_builtin_and_downloaded_are_independent():
    found = await _discover(None, [], failing={"local"})

    assert "local" not in found
    assert "local-downloaded" in found


async def test_rejects_host_with_any_failed_result_or_no_results():
    def results(target):
        if target.name == "local":
            return [_ok("local"), _failed("local")]
        if target.name == "local-downloaded":
            return []
        return [_ok(target.name)]

    found = await _discover(None, [], results=results)

    assert set(found) == {"web-eval-rhel"}


async def test_matches_results_by_target_not_by_result_host_label():
    """Container results label host as container:IMAGE@ARCH, not the inventory name."""

    def results(target):
        label = (
            f"container:{target.image}@{target.arch}" if target.kind == "container" else target.name
        )
        return [_ok(label)]

    found = await _discover(FakeImages(), [], results=results)

    assert {"ubuntu-26-04-arm64", "ubi10-arm64"} <= set(found)


# --- remote defaults (nothing relevant pulled locally) -----------------------


async def test_remote_family_chains_when_nothing_pulled():
    seen: list = []
    found = await _discover(FakeImages(local=["postgres:16"]), seen)

    assert set(found) == BASE | {"ubuntu-26-04-arm64", "ubi10-arm64"}
    assert found["ubuntu-26-04-arm64"] == {
        "transport": "container",
        "image": "ubuntu:26.04",
        "arch": "arm64",
        "qna_version": "11.0",
    }
    assert found["ubi10-arm64"]["image"] == UBI10
    assert not any("postgres" in name for name in seen)


async def test_remote_family_falls_back_when_newest_fails():
    seen: list = []
    found = await _discover(FakeImages(), seen, failing={"ubuntu-26-04-arm64", "ubi10-arm64"})

    assert {"ubuntu-24-04-arm64", "ubi9-arm64"} <= set(found)
    assert seen.index("ubuntu-26-04-arm64") < seen.index("ubuntu-24-04-arm64")
    assert found["ubi9-arm64"]["image"] == UBI9


async def test_remote_family_stops_at_first_that_works():
    seen: list = []
    await _discover(FakeImages(), seen)

    assert "ubuntu-24-04-arm64" not in seen
    assert "ubi9-arm64" not in seen


async def test_remote_family_all_failing_adds_nothing_for_it():
    found = await _discover(FakeImages(), [], failing={"ubuntu-26-04-arm64", "ubuntu-24-04-arm64"})

    assert not any(name.startswith("ubuntu") for name in found)
    assert "ubi10-arm64" in found


# --- locally present images are preferred ------------------------------------


async def test_local_debian_and_ubuntu_both_added_and_remote_not_tried():
    seen: list = []
    images = FakeImages(local=["ubuntu:24.04", "debian:12", "debian:11", "postgres:16"])
    found = await _discover(images, seen)

    assert {"ubuntu-24-04-arm64", "debian-12-arm64"} <= set(found)
    assert "ubuntu-26-04-arm64" not in seen
    assert "debian-11-arm64" not in seen  # newest debian worked first


async def test_local_tags_tried_newest_first():
    seen: list = []
    images = FakeImages(local=["debian:11", "debian:13", "debian:12"])
    await _discover(images, seen, failing={"debian-13-arm64"})

    debian = [name for name in seen if name.startswith("debian")]
    assert debian == ["debian-13-arm64", "debian-12-arm64"]


async def test_local_family_all_failing_falls_back_to_remote():
    seen: list = []
    images = FakeImages(local=["ubuntu:22.04"])
    found = await _discover(images, seen, failing={"ubuntu-22-04-arm64"})

    assert "ubuntu-26-04-arm64" in found
    assert seen.index("ubuntu-22-04-arm64") < seen.index("ubuntu-26-04-arm64")


async def test_local_rhel_family_image_preferred_over_ubi_download():
    seen: list = []
    images = FakeImages(local=["almalinux:9", "rockylinux:9"])
    found = await _discover(images, seen)

    assert {"almalinux-9-arm64", "rockylinux-9-arm64"} <= set(found)
    assert "ubi10-arm64" not in seen


async def test_local_ubi_versions_compare_by_repo_number():
    seen: list = []
    images = FakeImages(local=[UBI9 + ":latest", UBI10 + ":latest"])
    found = await _discover(images, seen)

    assert "ubi10-arm64" in found
    assert "ubi9-arm64" not in seen


# --- per-image architecture --------------------------------------------------


async def test_manifest_archs_fan_out_one_chain_per_arch():
    seen: list = []
    images = FakeImages(platforms={"ubuntu:26.04": {"x86_64", "arm64"}, UBI10: {"x86_64"}})
    found = await _discover(images, seen)

    assert {"ubuntu-26-04-arm64", "ubuntu-26-04-x86_64", "ubi10-x86_64"} <= set(found)
    assert found["ubuntu-26-04-x86_64"]["arch"] == "x86_64"
    assert "ubi10-arm64" not in seen


async def test_unknown_platforms_mean_host_arch_only():
    seen: list = []
    found = await _discover(FakeImages(), seen, arch="x86_64")

    assert {"ubuntu-26-04-x86_64", "ubi10-x86_64"} <= set(found)
    assert not any(name.endswith("-arm64") for name in seen)


async def test_image_lacking_an_arch_is_skipped_in_that_arch_chain():
    seen: list = []
    images = FakeImages(platforms={"ubuntu:26.04": {"x86_64"}, "ubuntu:24.04": {"x86_64", "arm64"}})
    found = await _discover(images, seen)

    assert "ubuntu-26-04-arm64" not in seen
    assert {"ubuntu-26-04-x86_64", "ubuntu-24-04-arm64"} <= set(found)


# --- re-runs -----------------------------------------------------------------


async def test_skip_known_names():
    seen: list = []
    found = await _discover(None, seen, skip={"local"})

    assert "local" not in seen
    assert set(found) == {"local-downloaded", "web-eval-rhel"}


async def test_family_already_represented_for_an_arch_is_skipped():
    seen: list = []
    await _discover(FakeImages(), seen, skip={"ubuntu-24-04-arm64"})

    assert not any(name.startswith("ubuntu") for name in seen)


async def test_pre_arch_suffix_names_count_as_represented_for_host_arch():
    """Inventories written before the -ARCH suffix used the host arch."""
    seen: list = []
    await _discover(FakeImages(), seen, skip={"ubuntu-26-04", "ubi9"})

    assert not any(name.startswith(("ubuntu", "ubi")) for name in seen)


# --- writing -----------------------------------------------------------------


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


# --- EngineImageSource over a fake docker SDK client ---------------------------


class _FakeRegistryData:
    def __init__(self, platforms) -> None:
        self.attrs = {"Platforms": platforms}

    def has_platform(self, platform):  # the SDK's strict variant match
        raise AssertionError("must not rely on has_platform: it misses arm64/v8")


class _FakeImagesApi:
    def __init__(self, tags, platforms) -> None:
        self._tags = tags
        self._platforms = platforms

    def list(self):
        return [type("Image", (), {"tags": tags})() for tags in self._tags]

    def get_registry_data(self, name):
        if name not in self._platforms:
            raise RuntimeError("registry unreachable")
        return _FakeRegistryData(self._platforms[name])


def _client(tags=(), platforms=None):
    return type("Client", (), {"images": _FakeImagesApi(list(tags), platforms or {})})()


def test_engine_source_lists_local_tags():
    source = EngineImageSource(_client(tags=[["ubuntu:24.04", "ubuntu:noble"], []]))

    assert source.local_tags() == ["ubuntu:24.04", "ubuntu:noble"]


def test_engine_source_platforms_include_arm64_v8_and_ignore_unknown():
    manifest = [
        {"os": "linux", "architecture": "amd64"},
        {"os": "unknown", "architecture": "unknown"},
        {"os": "linux", "architecture": "arm", "variant": "v7"},
        {"os": "linux", "architecture": "arm64", "variant": "v8"},
        {"os": "linux", "architecture": "s390x"},
    ]
    source = EngineImageSource(_client(platforms={"debian:12": manifest}))

    assert source.platforms("debian:12") == {"x86_64", "arm64"}


def test_engine_source_platforms_unknown_when_registry_unreachable():
    assert EngineImageSource(_client()).platforms("debian:12") is None


# --- default_image_source: warn when an engine is installed but not running ----


class _DownEngine:
    def client(self):
        raise RuntimeError("cannot connect")


class _UpEngine:
    def client(self):
        return _client()


def test_default_source_uses_first_engine_that_answers():
    source = default_image_source(engines=[_DownEngine(), _UpEngine()], which=lambda _: None)

    assert isinstance(source, EngineImageSource)


def test_warns_when_engine_installed_but_not_running(caplog):
    source = default_image_source(
        engines=[_DownEngine(), _DownEngine()],
        which=lambda name: "/usr/bin/docker" if name == "docker" else None,
    )

    assert source is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "docker" in message
    assert "not running" in message
    assert "--auto-discovery" in message


def test_no_warning_when_no_engine_installed(caplog):
    source = default_image_source(engines=[_DownEngine()], which=lambda _: None)

    assert source is None
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# --- run_auto_discovery: prove the file is writable before discovering --------


@pytest.fixture
def discover_calls(monkeypatch):
    """Replace discover(); record calls and report one working host."""
    calls: list = []

    async def fake_discover(**kwargs):
        calls.append(kwargs)
        return {"local": {"transport": "local", "qna_version": []}}

    monkeypatch.setattr(discovery_module, "discover", fake_discover)
    return calls


def test_unwritable_location_fails_before_discovering(tmp_path, discover_calls):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")  # a file where ~/.bigfix should be
    path = blocker / "remote_clients.toml"

    with pytest.raises(InventoryError, match="cannot write"):
        run_auto_discovery(path)

    assert discover_calls == []


def test_writable_location_probe_leaves_no_empty_file_behind(tmp_path, monkeypatch):
    path = tmp_path / ".bigfix" / "remote_clients.toml"
    seen_during_discovery: list[bool] = []

    async def fake_discover(**kwargs):
        seen_during_discovery.append(path.exists())
        return {}

    monkeypatch.setattr(discovery_module, "discover", fake_discover)

    assert run_auto_discovery(path) == []
    assert seen_during_discovery == [False]
    # Afterwards it's the instructions placeholder, never an empty file.
    assert "--auto-discovery" in path.read_text(encoding="utf-8")


def test_writable_location_discovers_and_writes(tmp_path, discover_calls):
    path = tmp_path / ".bigfix" / "remote_clients.toml"

    assert run_auto_discovery(path) == ["local"]
    assert len(discover_calls) == 1
    assert "[hosts.local]" in path.read_text(encoding="utf-8")


def test_existing_read_only_file_fails_before_discovering(tmp_path, discover_calls, monkeypatch):
    path = tmp_path / "remote_clients.toml"
    path.write_text('[hosts.b]\ntransport = "ssh"\n', encoding="utf-8")
    real_open = open

    def deny_append(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and "a" in mode:
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny_append)

    with pytest.raises(InventoryError, match="cannot write"):
        run_auto_discovery(path)

    assert discover_calls == []


# --- nothing found: a comment-only placeholder ends first-run rediscovery ------


def _discover_returning(monkeypatch, found):
    async def fake_discover(**kwargs):
        return found

    monkeypatch.setattr(discovery_module, "discover", fake_discover)


def test_nothing_found_writes_comment_only_placeholder(tmp_path, monkeypatch):
    path = tmp_path / ".bigfix" / "remote_clients.toml"
    _discover_returning(monkeypatch, {})

    assert run_auto_discovery(path) == []

    text = path.read_text(encoding="utf-8")
    assert all(line.startswith("#") or not line.strip() for line in text.splitlines())
    assert tomllib.loads(text) == {}
    assert "--auto-discovery" in text
    with pytest.raises(EmptyInventoryError):
        load_inventory(path)


def test_nothing_found_leaves_existing_file_alone(tmp_path, monkeypatch):
    path = tmp_path / "remote_clients.toml"
    original = '# mine\n[hosts.b]\ntransport = "ssh"\n'
    path.write_text(original, encoding="utf-8")
    _discover_returning(monkeypatch, {})

    assert run_auto_discovery(path) == []
    assert path.read_text(encoding="utf-8") == original


def test_rediscovery_replaces_placeholder_text(tmp_path, monkeypatch):
    path = tmp_path / ".bigfix" / "remote_clients.toml"
    _discover_returning(monkeypatch, {})
    run_auto_discovery(path)
    _discover_returning(monkeypatch, {"local": {"transport": "local", "qna_version": []}})

    assert run_auto_discovery(path) == ["local"]

    text = path.read_text(encoding="utf-8")
    assert "found no working hosts" not in text
    assert "Created by bigfix-remote-client-relevance auto-discovery." in text
    assert tomllib.loads(text)["hosts"]["local"]["transport"] == "local"
