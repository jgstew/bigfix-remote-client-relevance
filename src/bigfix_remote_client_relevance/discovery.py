"""First-run auto-discovery of a usable ``~/.bigfix/remote_clients.toml``.

Runs when no inventory exists anywhere on the search path (see
:mod:`~bigfix_remote_client_relevance.inventory_paths`) and no target was
given, or on demand via ``--auto-discovery``. Each candidate host is
actually evaluated first; only a host whose every result came back without
an error is written, so the file never lists a target that cannot work.

Candidates:

* ``local`` -- the BigFix client on this machine, same as ``--local``.
* ``web-eval-rhel`` -- the developer.bigfix.com online evaluator.
* One debian-family and one rhel-family container -- only when a docker or
  podman CLI is on PATH: ``ubuntu:26.04`` falling back to ``ubuntu:24.04``,
  and UBI 10 falling back to UBI 9. Pinned to this machine's own
  architecture: 11.0.7 is the first release with native arm64 builds, so
  the ``11.0`` stream covers both.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import ParseError

from bigfix_remote_client_relevance.bootstrap.targets import host_arch
from bigfix_remote_client_relevance.inventory import InventoryError, _target_from_entry
from bigfix_remote_client_relevance.orchestrate import Target, evaluate_client_relevance
from bigfix_remote_client_relevance.results import ClientRelevanceResult

logger = logging.getLogger(__name__)

PROBE_RELEVANCE = "name of operating system"

# Generous: a container candidate's first run pulls an image and provisions qna.
DISCOVERY_TIMEOUT_S = 300.0

Evaluator = Callable[..., Awaitable[Sequence[ClientRelevanceResult]]]


def container_engine_available() -> bool:
    """A docker or podman CLI is on PATH. A stopped daemon just fails the probe."""
    return any(shutil.which(name) for name in ("docker", "podman"))


Candidate = tuple[str, dict[str, Any]]


def _container(image: str, arch: str) -> dict[str, Any]:
    return {"transport": "container", "image": image, "arch": arch, "qna_version": "11.0"}


def candidate_groups(*, engine_available: bool, arch: str) -> list[list[Candidate]]:
    """Inventory entries worth trying, as fallback chains.

    Each group is tried in order and stops at the first entry that works, so
    a family gets at most one host: its newest image, or the next one down
    when that fails (say, a new OS release qna can't run on yet).
    """
    groups: list[list[Candidate]] = [
        # Empty list, not a string: probe whatever qna is installed rather
        # than provisioning a pinned version.
        [("local", {"transport": "local", "qna_version": []})],
        [
            (
                "web-eval-rhel",
                {
                    "transport": "online_evaluator",
                    "base_url": "https://developer.bigfix.com",
                    "qna_version": [],
                    "arch": "x86_64",
                },
            )
        ],
    ]
    if engine_available:
        groups.append(  # debian family
            [
                ("ubuntu-26-04", _container("ubuntu:26.04", arch)),
                ("ubuntu-24-04", _container("ubuntu:24.04", arch)),
            ]
        )
        groups.append(  # rhel family
            [
                ("ubi10", _container("registry.access.redhat.com/ubi10/ubi", arch)),
                ("ubi9", _container("registry.access.redhat.com/ubi9/ubi", arch)),
            ]
        )
    return groups


async def discover(
    *,
    engine_available: bool | None = None,
    arch: str | None = None,
    evaluate: Evaluator | None = None,
    skip: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Try each candidate group; return the first working entry of each.

    A group with any member already in ``skip`` is skipped whole: that
    family is already represented in the inventory.
    """
    groups = candidate_groups(
        engine_available=(
            container_engine_available() if engine_available is None else engine_available
        ),
        arch=host_arch() if arch is None else arch,
    )
    groups = [group for group in groups if not any(name in skip for name, _ in group)]
    if not groups:
        return {}

    run = evaluate or evaluate_client_relevance
    logger.info(
        "auto-discovery: trying %s", ", ".join(" -> ".join(n for n, _ in g) for g in groups)
    )

    async def works(name: str, entry: dict[str, Any]) -> bool:
        # One call per target: a result's `host` is a display label
        # (container:IMAGE@ARCH for containers), not the inventory name, so
        # results can only be attributed by which call produced them.
        target: Target = _target_from_entry(name, entry, {}, Path("<auto-discovery>"))
        results = await run(PROBE_RELEVANCE, [target], timeout_s=DISCOVERY_TIMEOUT_S)
        ok = bool(results) and all(r.error_kind is None for r in results)
        if not ok:
            reason = next((r.error for r in results if r.error), "no result")
            logger.info("auto-discovery: %s did not work (%s)", name, reason)
        return ok

    async def first_working(group: list[Candidate]) -> Candidate | None:
        # Sequential within a group, so a fallback image is only pulled when
        # the preferred one actually failed; groups still run concurrently.
        for name, entry in group:
            if await works(name, entry):
                return name, entry
        return None

    winners = await asyncio.gather(*(first_working(group) for group in groups))
    return dict(winner for winner in winners if winner is not None)


def write_discovered(path: Path, hosts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Add ``hosts`` not already in ``path``'s ``[hosts]``; return the names added.

    Creates the file (and its directory) only when there is something to
    add. An existing file is edited with tomlkit so comments survive, and an
    existing host is never overwritten.
    """
    if path.is_file():
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except (OSError, ParseError, UnicodeDecodeError) as exc:
            raise InventoryError(f"could not read inventory {path}: {exc}") from exc
    else:
        document = tomlkit.document()

    existing = document.get("hosts")
    known = set(existing) if existing is not None else set()
    added = [name for name in hosts if name not in known]
    if not added:
        return []

    if existing is None:
        if not path.is_file():
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


def run_auto_discovery(path: Path, **kwargs: Any) -> list[str]:
    """Discover hosts missing from ``path`` and write the working ones to it."""
    skip: set[str] = set()
    if path.is_file():
        try:
            hosts = tomlkit.parse(path.read_text(encoding="utf-8")).get("hosts")
        except (OSError, ParseError, UnicodeDecodeError) as exc:
            raise InventoryError(f"could not read inventory {path}: {exc}") from exc
        skip = set(hosts) if hosts is not None else set()
    found = asyncio.run(discover(skip=skip, **kwargs))
    return write_discovered(path, found)


__all__ = [
    "PROBE_RELEVANCE",
    "candidate_groups",
    "container_engine_available",
    "discover",
    "run_auto_discovery",
    "write_discovered",
]
