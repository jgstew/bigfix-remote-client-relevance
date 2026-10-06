"""First-run auto-discovery of a usable ``~/.bigfix/remote_clients.toml``.

Runs when no inventory exists anywhere on the search path (see
:mod:`~bigfix_remote_client_relevance.inventory_paths`) and no target was
given, or on demand via ``--auto-discovery``. Each candidate host is
actually evaluated first; only a host whose every result came back without
an error is written, so the file never lists a target that cannot work.

Candidates:

* ``local`` -- the BigFix client on this machine with its own installed qna,
  same as ``--local``; and ``local-downloaded``, the same machine with qna
  11.0 downloaded, so the two can be compared.
* ``web-eval-rhel`` -- the developer.bigfix.com online evaluator.
* Containers, when a docker or podman engine answers: at least one
  debian-family and one rhel-family host per architecture. Images already
  pulled are preferred -- one host per distro (``ubuntu`` and ``debian``
  both, say), newest tag first -- and only when none of a family's local
  images works is a default downloaded: ``ubuntu:26.04`` falling back to
  ``ubuntu:24.04``, and UBI 10 falling back to UBI 9. Each image is tried
  for every architecture its registry manifest publishes (x86_64, arm64),
  or just this machine's when the registry can't be asked. qna 11.0.7 is
  the first release with native arm64 builds for most distros, so the
  ``11.0`` stream covers both.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tomllib
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import tomlkit
from tomlkit.exceptions import ParseError

from bigfix_remote_client_relevance.bootstrap.targets import host_arch, normalize_arch
from bigfix_remote_client_relevance.inventory import InventoryError, _target_from_entry
from bigfix_remote_client_relevance.orchestrate import Target, evaluate_client_relevance
from bigfix_remote_client_relevance.results import ClientRelevanceResult

logger = logging.getLogger(__name__)

PROBE_RELEVANCE = "name of operating system"

# Generous: a container candidate's first run pulls an image and provisions qna.
DISCOVERY_TIMEOUT_S = 300.0

QNA_STREAM = "11.0"

SUPPORTED_ARCHS = ("x86_64", "arm64")

UBI10 = "registry.access.redhat.com/ubi10/ubi"
UBI9 = "registry.access.redhat.com/ubi9/ubi"

# Image base name (see _base) -> family. Only these are ever considered
# among locally pulled images; anything else (postgres, this tool's own
# bfrcr/* images, ...) is ignored.
_FAMILY_OF_DISTRO = {
    "ubuntu": "debian",
    "debian": "debian",
    "ubi": "rhel",
    "almalinux": "rhel",
    "rockylinux": "rhel",
    "oraclelinux": "rhel",
    "amazonlinux": "rhel",
    "fedora": "rhel",
    "centos": "rhel",
}

# Downloaded only when no locally pulled image of the family works.
_REMOTE_DEFAULTS = {
    "debian": ("ubuntu:26.04", "ubuntu:24.04"),
    "rhel": (UBI10, UBI9),
}

Evaluator = Callable[..., Awaitable[Sequence[ClientRelevanceResult]]]
Candidate = tuple[str, dict[str, Any]]


class ImageSource(Protocol):
    """What discovery needs from a container engine and its registries."""

    def local_tags(self) -> list[str]:
        """Every ``repo:tag`` already pulled."""
        ...

    def platforms(self, image: str) -> set[str] | None:
        """Archs (canonical spelling) ``image``'s manifest publishes; None if unknown."""
        ...


class EngineImageSource:
    """:class:`ImageSource` over a docker SDK client (docker or podman)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def local_tags(self) -> list[str]:
        return [tag for image in self._client.images.list() for tag in image.tags]

    def platforms(self, image: str) -> set[str] | None:
        try:
            data = self._client.images.get_registry_data(image)
        except Exception as exc:  # noqa: BLE001 - offline or local-only: just unknown
            logger.debug("no registry data for %s: %s", image, exc)
            return None
        # Read the manifest list directly rather than data.has_platform(): that
        # matches the variant exactly, so "linux/arm64" misses arm64/v8 --
        # which is how nearly every image publishes it.
        archs = {
            normalize_arch(str(entry.get("architecture", "")))
            for entry in data.attrs.get("Platforms", [])
            if entry.get("os") == "linux"
        }
        return archs & set(SUPPORTED_ARCHS)


class _Engine(Protocol):
    def client(self) -> object: ...


def default_image_source(
    *,
    engines: Sequence[_Engine] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ImageSource | None:
    """docker, else podman, if either answers right now; else None.

    ``auto_setup=False``: discovery never starts a stopped engine just to
    look -- with nothing answering, there are simply no container candidates.
    That is easy to miss on a first run, so an engine that is installed but
    not running gets a warning saying how to add containers later.
    """
    if engines is None:
        from bigfix_remote_client_relevance.transports.container import (
            DockerEngine,
            PodmanEngine,
        )

        engines = [DockerEngine(auto_setup=False), PodmanEngine(auto_setup=False)]
    for engine in engines:
        try:
            return EngineImageSource(engine.client())
        except Exception as exc:  # noqa: BLE001 - try the next engine
            logger.debug("auto-discovery: no container engine: %s", exc)

    installed = [name for name in ("docker", "podman") if which(name)]
    if installed:
        logger.warning(
            "auto-discovery: %s is installed but not running, so no container hosts "
            "were tried; start it and re-run with --auto-discovery to add them",
            " / ".join(installed),
        )
    return None


def _split(image: str) -> tuple[str, str]:
    """``repo:tag`` -> (repo, tag); the tag is "" when absent (a registry port isn't one)."""
    repo, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        return image, ""
    return repo, tag


def _base(repo: str) -> str:
    """``registry.access.redhat.com/ubi10/ubi`` -> ``ubi10``; ``docker.io/library/debian`` -> ``debian``."""
    parts = repo.lower().split("/")
    if parts[-1] == "ubi" and len(parts) > 1 and parts[-2].startswith("ubi"):
        return parts[-2]
    return parts[-1]


def _distro(image: str) -> str | None:
    base = _base(_split(image)[0])
    key = "ubi" if re.fullmatch(r"ubi\d*", base) else base
    return key if key in _FAMILY_OF_DISTRO else None


def host_name_for(image: str, arch: str) -> str:
    """Inventory host name: ``ubuntu:26.04`` + arm64 -> ``ubuntu-26-04-arm64``."""
    repo, tag = _split(image)
    stem = _base(repo) if tag in ("", "latest") else f"{_base(repo)}-{tag}"
    return f"{re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')}-{arch}"


def _newest_first(images: Collection[str]) -> list[str]:
    """Numerically by every number in base name + tag; ``latest`` and friends last."""

    def key(image: str) -> tuple[int, ...]:
        repo, tag = _split(image)
        return tuple(int(n) for n in re.findall(r"\d+", f"{_base(repo)}:{tag}"))

    return sorted(dict.fromkeys(images), key=key, reverse=True)


def _image_key(image: str) -> str:
    """One spelling per image: ``docker.io/library/debian`` == ``debian:latest``."""
    image = image.strip().lower()
    for prefix in ("docker.io/library/", "index.docker.io/library/", "docker.io/"):
        if image.startswith(prefix):
            image = image[len(prefix) :]
            break
    repo, tag = _split(image)
    return f"{repo}:{tag or 'latest'}"


def _qna_key(version: object) -> tuple[str, ...]:
    """``[]``, ``None`` and ``""`` all mean the installed qna."""
    if isinstance(version, str):
        return (version,) if version else ()
    if isinstance(version, (list, tuple)):
        return tuple(str(v) for v in version)
    return ()


def _identity(entry: Mapping[str, Any]) -> tuple[str, ...] | None:
    """What an inventory entry *is*, regardless of its name.

    Two entries with the same identity evaluate the same way, so discovery
    must not add one when the other is already there under another name.
    ``entry`` must already have ``[defaults]`` applied.
    """
    transport = str(entry.get("transport", "ssh"))
    if transport == "container" and entry.get("image"):
        # No arch on a container means x86_64 -- see default_transport_factory.
        arch = normalize_arch(str(entry.get("arch") or "x86_64"))
        return ("container", _image_key(str(entry["image"])), arch)
    if transport == "local":
        return ("local", *_qna_key(entry.get("qna_version")))
    if transport == "online_evaluator" and entry.get("base_url"):
        return ("online_evaluator", str(entry["base_url"]).rstrip("/").lower())
    return None


def _container(image: str, arch: str) -> dict[str, Any]:
    return {"transport": "container", "image": image, "arch": arch, "qna_version": QNA_STREAM}


def _base_candidates() -> list[Candidate]:
    return [
        # Empty list, not a string: probe whatever qna is installed rather
        # than provisioning a pinned version.
        ("local", {"transport": "local", "qna_version": []}),
        ("local-downloaded", {"transport": "local", "qna_version": QNA_STREAM}),
        (
            "web-eval-rhel",
            {
                "transport": "online_evaluator",
                "base_url": "https://developer.bigfix.com",
                "qna_version": [],
                "arch": "x86_64",
            },
        ),
    ]


class _Discovery:
    """One discovery run: shared evaluator, what's already known, image metadata."""

    def __init__(
        self,
        run: Evaluator,
        known: set[str],
        existing: Mapping[str, Mapping[str, Any]],
        arch: str,
    ) -> None:
        self._run = run
        self._known = known
        self._known_ids = {
            identity for entry in existing.values() if (identity := _identity(entry))
        }
        # (distro, arch) pairs some existing container entry already covers,
        # whatever its tag or name: ubuntu-2404 covers ubuntu on x86_64.
        self._known_distros = {
            (_distro(identity[1]), identity[2])
            for identity in self._known_ids
            if identity[0] == "container"
        }
        self._arch = arch
        self._platforms: dict[str, set[str] | None] = {}

    async def works(self, name: str, entry: dict[str, Any]) -> bool:
        # One call per target: a result's `host` is a display label
        # (container:IMAGE@ARCH for containers), not the inventory name, so
        # results can only be attributed by which call produced them.
        target: Target = _target_from_entry(name, entry, {}, Path("<auto-discovery>"))
        results = await self._run(PROBE_RELEVANCE, [target], timeout_s=DISCOVERY_TIMEOUT_S)
        ok = bool(results) and all(r.error_kind is None for r in results)
        if not ok:
            reason = next((r.error for r in results if r.error), "no result")
            logger.info("auto-discovery: %s did not work (%s)", name, reason)
        return ok

    async def first_working(self, chain: Sequence[Candidate]) -> Candidate | None:
        # Sequential, so a fallback is only tried (and pulled) when the
        # preferred entry actually failed.
        for name, entry in chain:
            if await self.works(name, entry):
                return name, entry
        return None

    def is_known(self, candidate: Candidate) -> bool:
        """``candidate`` is already in the inventory, by name or by what it is."""
        name, entry = candidate
        return name in self._known or _identity(entry) in self._known_ids

    def _represented(self, images: Collection[str], arch: str) -> bool:
        """``images``' distros already in the inventory for ``arch``."""
        if any((_distro(image), arch) in self._known_distros for image in images):
            return True
        for image in images:
            name = host_name_for(image, arch)
            # Inventories written before the -ARCH suffix used the host arch.
            if name in self._known or (
                arch == self._arch and name.removesuffix(f"-{arch}") in self._known
            ):
                return True
        return False

    def _archs(self, image: str) -> set[str]:
        published = self._platforms.get(image)
        if published is None:
            return {self._arch}
        return published & set(SUPPORTED_ARCHS)

    def _chain(self, images: Sequence[str], arch: str) -> list[Candidate]:
        return [
            (host_name_for(image, arch), _container(image, arch))
            for image in images
            if arch in self._archs(image)
        ]

    async def single(self, candidate: Candidate) -> list[Candidate]:
        winner = await self.first_working([candidate])
        return [winner] if winner is not None else []

    async def family(
        self, images: ImageSource, family: str, local: Mapping[str, list[str]]
    ) -> list[Candidate]:
        """At least one working host of ``family`` per arch, local images first."""
        remote = _REMOTE_DEFAULTS[family]
        every = [*(image for tags in local.values() for image in tags), *remote]
        found = await asyncio.gather(*(asyncio.to_thread(images.platforms, i) for i in every))
        self._platforms.update(zip(every, found, strict=True))
        archs = sorted(set().union(*(self._archs(image) for image in every)))

        async def per_arch(arch: str) -> list[Candidate]:
            chains = [tags for tags in local.values() if not self._represented(tags, arch)]
            won = [
                winner
                for winner in await asyncio.gather(
                    *(self.first_working(self._chain(tags, arch)) for tags in chains)
                )
                if winner is not None
            ]
            if won or self._represented(every, arch):
                return won
            winner = await self.first_working(self._chain(remote, arch))
            return [winner] if winner is not None else []

        per = await asyncio.gather(*(per_arch(arch) for arch in archs))
        return [candidate for candidates in per for candidate in candidates]


_DEFAULT_IMAGES: Any = object()


async def discover(
    *,
    images: ImageSource | None = _DEFAULT_IMAGES,
    arch: str | None = None,
    evaluate: Evaluator | None = None,
    skip: Collection[str] = (),
    existing: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Try every candidate not already known; return the ones that worked.

    Known means named in ``skip``, or the same as an ``existing`` entry
    (``[defaults]`` already applied) under any name -- see ``_identity``.

    ``images`` is the container engine to look in; None means there isn't
    one (no container candidates at all). Left out, docker or podman is
    looked for.
    """
    if images is _DEFAULT_IMAGES:
        images = await asyncio.to_thread(default_image_source)
    run = _Discovery(
        evaluate or evaluate_client_relevance,
        set(skip),
        existing or {},
        host_arch() if arch is None else arch,
    )

    tasks: list[Awaitable[list[Candidate]]] = [
        run.single(candidate) for candidate in _base_candidates() if not run.is_known(candidate)
    ]
    if images is not None:
        local: dict[str, dict[str, list[str]]] = {family: {} for family in _REMOTE_DEFAULTS}
        for tag in await asyncio.to_thread(images.local_tags):
            distro = _distro(tag)
            if distro is not None:
                local[_FAMILY_OF_DISTRO[distro]].setdefault(distro, []).append(tag)
        for family, distros in local.items():
            ordered = {distro: _newest_first(tags) for distro, tags in distros.items()}
            if ordered:
                logger.info("auto-discovery: pulled %s images: %s", family, ordered)
            tasks.append(run.family(images, family, ordered))

    found: dict[str, dict[str, Any]] = {}
    for winners in await asyncio.gather(*tasks):
        found.update(winners)
    return found


PLACEHOLDER_MARKER = "# Auto-discovery found no working hosts"

PLACEHOLDER = f"""\
# Created by bigfix-remote-client-relevance auto-discovery.
{PLACEHOLDER_MARKER}, so this file has no [hosts.*]
# entries yet, and runs that give no target will fail until it does.
#
# Either add hosts by hand (see remote_clients.example.toml in the project),
# or fix whatever kept discovery from working -- e.g. install the BigFix
# client, start docker or podman, or check network access -- and re-run:
#
#   bigfix-remote-client-relevance --auto-discovery
#
# Re-running replaces this text with what it finds.
"""


def write_placeholder(path: Path) -> None:
    """Write the comment-only file left behind when discovery finds nothing.

    Its existence is what stops first-run discovery from repeating on every
    run; loading it raises EmptyInventoryError, whose message suggests
    ``--auto-discovery``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLACEHOLDER, encoding="utf-8", newline="")
    logger.warning(
        "auto-discovery found no working hosts; wrote %s with instructions instead", path
    )


def write_discovered(path: Path, hosts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Add ``hosts`` not already in ``path``'s ``[hosts]``; return the names added.

    Creates the file (and its directory) only when there is something to
    add. An existing file is edited with tomlkit so comments survive, and an
    existing host is never overwritten.
    """
    fresh = True
    document = tomlkit.document()
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
            parsed = tomlkit.parse(text)
        except (OSError, ParseError, UnicodeDecodeError) as exc:
            raise InventoryError(f"could not read inventory {path}: {exc}") from exc
        # The nothing-found placeholder is replaced, not appended to: its
        # instructions would be wrong as soon as there are hosts.
        if not (PLACEHOLDER_MARKER in text and parsed.get("hosts") is None):
            document = parsed
            fresh = False

    existing = document.get("hosts")
    known = set(existing) if existing is not None else set()
    added = [name for name in hosts if name not in known]
    if not added:
        return []

    if existing is None:
        if fresh:
            document.add(
                tomlkit.comment("Created by bigfix-remote-client-relevance auto-discovery.")
            )
            document.add(
                tomlkit.comment("Re-run with --auto-discovery to add newly available hosts.")
            )
        existing = tomlkit.table(is_super_table=True)
        document["hosts"] = existing
    for name in added:
        table = tomlkit.table()
        for key, value in hosts[name].items():
            table[key] = value
        existing[name] = table

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document), encoding="utf-8", newline="")
    logger.info("auto-discovery: added %s to %s", ", ".join(added), path)
    return added


def ensure_writable(path: Path) -> None:
    """Prove ``path`` can be written, creating its directory, before discovery.

    Discovery can take minutes, and first-run discovery repeats on every run
    until the file exists -- so a location that can never be written would
    rediscover forever. Fail up front instead. A file that doesn't exist yet
    is created and removed again rather than left empty: an empty file is a
    "found" inventory with no hosts, which would stop first-run discovery
    without giving it anything to use.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with open(path, "a", encoding="utf-8"):
                pass
        else:
            with open(path, "x", encoding="utf-8"):
                pass
            path.unlink()
    except OSError as exc:
        raise InventoryError(
            f"cannot write {path}, so auto-discovery would have nowhere to save its results: {exc}"
        ) from exc


def run_auto_discovery(path: Path, **kwargs: Any) -> list[str]:
    """Discover hosts missing from ``path`` and write the working ones to it."""
    ensure_writable(path)
    existing: dict[str, dict[str, Any]] = {}
    if path.is_file():
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise InventoryError(f"could not read inventory {path}: {exc}") from exc
        defaults = document.get("defaults", {})
        # Per-host values win over [defaults], same as load_inventory.
        existing = {
            name: {**defaults, **(config or {})}
            for name, config in document.get("hosts", {}).items()
        }
    found = asyncio.run(discover(skip=set(existing), existing=existing, **kwargs))
    added = write_discovered(path, found)
    if not added and not path.exists():
        write_placeholder(path)
    return added


__all__ = [
    "PLACEHOLDER",
    "PROBE_RELEVANCE",
    "EngineImageSource",
    "ImageSource",
    "default_image_source",
    "discover",
    "ensure_writable",
    "host_name_for",
    "run_auto_discovery",
    "write_discovered",
    "write_placeholder",
]
