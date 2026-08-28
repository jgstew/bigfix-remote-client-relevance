"""Load a hosts.toml inventory into orchestration targets.

Example::

    [defaults]
    qna_version = "11.0"        # version spec; overridable per host

    [hosts.mac-test]            # table name is the ~/.ssh/config alias
    transport = "ssh"
    become = true               # sudo for root-only inspectors

    [hosts.ubuntu-22]
    transport = "container"
    image = "ubuntu:22.04"
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import tomlkit

from bigfix_remote_client_relevance.orchestrate import Target

logger = logging.getLogger(__name__)

KNOWN_TRANSPORTS = frozenset({"ssh", "local", "container", "fastquery"})

DEFAULT_TRANSPORT = "ssh"


class InventoryError(Exception):
    """The inventory file is missing, malformed, or describes an unusable host."""


def load_inventory(path: str | Path) -> list[Target]:
    """Read ``path`` and return one :class:`Target` per host."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"could not read inventory {path}: {exc}") from exc

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise InventoryError(f"could not parse inventory {path}: {exc}") from exc

    defaults = document.get("defaults", {})
    hosts = document.get("hosts", {})
    if not hosts:
        raise InventoryError(f"inventory {path} defines no [hosts.*] entries")

    targets: list[Target] = []
    for name, config in hosts.items():
        targets.append(_target_from_entry(name, config or {}, defaults, path))
    logger.debug("loaded %d target(s) from %s", len(targets), path)
    return targets


def _target_from_entry(
    name: str, config: dict[str, object], defaults: dict[str, object], path: Path
) -> Target:
    def setting(key: str, fallback: object = None) -> object:
        # Per-host values win over [defaults].
        if key in config:
            return config[key]
        return defaults.get(key, fallback)

    kind = str(setting("transport", DEFAULT_TRANSPORT))
    if kind not in KNOWN_TRANSPORTS:
        known = ", ".join(sorted(KNOWN_TRANSPORTS))
        raise InventoryError(
            f"host {name!r} in {path} has unknown transport {kind!r}; known: {known}"
        )

    image = setting("image")
    if kind == "container" and not image:
        raise InventoryError(f"container host {name!r} in {path} needs an `image`")

    user = setting("user")
    version = setting("qna_version")
    platform = setting("platform")
    become_raw = setting("become")

    return Target(
        kind=kind,
        name=name,
        user=str(user) if user is not None else None,
        # Left as None when unset (rather than coerced to False) so
        # default_transport_factory can imply --become for a `local` host on
        # a macOS controller -- the same default the CLI's --local gets.
        become=bool(become_raw) if become_raw is not None else None,
        image=str(image) if image is not None else None,
        arch=str(setting("arch", "x86_64")),
        platform=str(platform) if platform is not None else None,
        qna_version=version if isinstance(version, (str, list)) else None,
        keep_alive=bool(setting("keep_alive", False)),
        auto_setup=bool(setting("auto_setup", True)),
        verify_host_key=bool(setting("verify_host_key", True)),
    )


def update_inventory_platform(path: str | Path, host: str, platform: str) -> None:
    """Write a probed or corrected ``platform`` into one host's table.

    Uses ``tomlkit`` rather than ``tomllib`` (read-only) plus a plain-dict
    rewrite: a naive rewrite would silently drop every comment and could
    reorder tables, and this is exactly the kind of file a person hand-edits
    and keeps under version control. Only the one host's ``platform`` key
    changes; everything else in the file is untouched byte-for-byte.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"could not read inventory {path}: {exc}") from exc

    try:
        document = tomlkit.parse(raw.decode("utf-8"))
    except tomlkit.exceptions.ParseError as exc:
        raise InventoryError(f"could not parse inventory {path}: {exc}") from exc

    hosts = document.get("hosts", {})
    if host not in hosts:
        raise InventoryError(f"no host {host!r} in inventory {path}")

    hosts[host]["platform"] = platform
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    logger.info("wrote platform = %r for %r to %s", platform, host, path)


__all__ = [
    "DEFAULT_TRANSPORT",
    "KNOWN_TRANSPORTS",
    "InventoryError",
    "load_inventory",
    "update_inventory_platform",
]
