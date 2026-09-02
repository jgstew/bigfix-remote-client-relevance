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

    [hosts.web-eval]
    transport = "online_evaluator"
    base_url = "https://developer.bigfix.com"
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from bigfix_remote_client_relevance.exceptions import BigFixRelevanceError
from bigfix_remote_client_relevance.orchestrate import Target

logger = logging.getLogger(__name__)

KNOWN_TRANSPORTS = frozenset({"ssh", "local", "container", "fastquery", "online_evaluator"})

DEFAULT_TRANSPORT = "ssh"


class InventoryError(BigFixRelevanceError):
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


def _optional_float(value: object) -> float | None:
    """A numeric inventory setting, or None when it is absent or unusable.

    Unusable rather than fatal: an idle window is a tuning knob, and a
    mistyped one should fall back to the default, not refuse to load the
    whole inventory.
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("ignoring non-numeric idle_ttl_s %r", value)
        return None


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

    base_url = setting("base_url")
    if kind == "online_evaluator" and not base_url:
        raise InventoryError(f"online_evaluator host {name!r} in {path} needs a `base_url`")

    user = setting("user")
    version = setting("qna_version")
    platform = setting("platform")
    arch = setting("arch")
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
        base_url=str(base_url) if base_url is not None else None,
        # None (rather than a hardcoded default) when unset, same as
        # `platform` below: ssh/local targets get it probed (see
        # orchestrate._one()); a container needs an explicit one, which the
        # CLI always supplies.
        arch=str(arch) if arch is not None else None,
        platform=str(platform) if platform is not None else None,
        qna_version=version if isinstance(version, (str, list)) else None,
        keep_alive=bool(setting("keep_alive", False)),
        idle_ttl_s=_optional_float(setting("idle_ttl_s", None)),
        auto_setup=bool(setting("auto_setup", True)),
        verify_host_key=bool(setting("verify_host_key", True)),
    )


def _write_inventory_key(path: str | Path, host: str, key: str, value: str) -> None:
    """Write a probed or corrected ``key`` into one host's table.

    Uses ``tomlkit`` rather than ``tomllib`` (read-only) plus a plain-dict
    rewrite: a naive rewrite would silently drop every comment and could
    reorder tables, and this is exactly the kind of file a person hand-edits
    and keeps under version control. Only the one host's ``key`` changes;
    everything else in the file is untouched byte-for-byte.

    Shared by :func:`update_inventory_platform` and
    :func:`update_inventory_arch` -- the only difference between them is
    which key gets written.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"could not read inventory {path}: {exc}") from exc

    try:
        document = tomlkit.parse(raw.decode("utf-8"))
    except ParseError as exc:
        raise InventoryError(f"could not parse inventory {path}: {exc}") from exc

    hosts = document.get("hosts", {})
    if host not in hosts:
        raise InventoryError(f"no host {host!r} in inventory {path}")

    hosts[host][key] = value
    # newline="" disables translation on write: tomlkit already reproduces the
    # file's own line endings, and re-translating them would turn a CRLF file
    # into a \r\r\n one that tomllib then refuses to read back.
    path.write_text(tomlkit.dumps(document), encoding="utf-8", newline="")
    logger.info("wrote %s = %r for %r to %s", key, value, host, path)


def update_inventory_platform(path: str | Path, host: str, platform: str) -> None:
    """Write a probed or corrected ``platform`` into one host's table."""
    _write_inventory_key(path, host, "platform", platform)


def update_inventory_arch(path: str | Path, host: str, arch: str) -> None:
    """Write a probed ``arch`` into one host's table."""
    _write_inventory_key(path, host, "arch", arch)


__all__ = [
    "DEFAULT_TRANSPORT",
    "KNOWN_TRANSPORTS",
    "InventoryError",
    "load_inventory",
    "update_inventory_arch",
    "update_inventory_platform",
]
