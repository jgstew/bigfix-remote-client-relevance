"""Locating the default remote_clients.toml inventory.

Mirrors :mod:`~bigfix_remote_client_relevance.qna_paths`'s shape: a
:func:`default_candidates` builder plus a :func:`find_inventory_path` that
returns the first existing file, or ``None``. Used only for the zero-argument
auto-discovery case -- an explicit ``--inventory PATH`` never goes through
this module.

Search order, current directory first:

1. ``./remote_clients.toml`` -- unchanged behavior from the plain
   current-directory check this replaces.
2. ``~/.bigfix/remote_clients.toml`` -- per-user, a literal dotfolder in the
   home directory on every OS (same convention as ``~/.ssh``, ``~/.aws``,
   ``~/.docker``). ``.bigfix`` is deliberately a shared, cross-project folder
   name -- other BigFix tools can drop their own config under it too, not
   just this one.
3. The platform's all-users config directory, via
   ``platformdirs.site_config_dir("bigfix")`` -- ``/etc/xdg/bigfix`` on
   Linux, ``/Library/Application Support/bigfix`` on macOS,
   ``C:\\ProgramData\\bigfix`` on Windows. Called with the shared
   ``"bigfix"`` name, not this package's own name, for the same
   cross-project reason as ``.bigfix`` above.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

DEFAULT_INVENTORY_FILENAME = "remote_clients.toml"

SHARED_CONFIG_DIR_NAME = "bigfix"
"""The cross-project folder/app name other BigFix tools can share -- not this
package's own name (``bigfix_remote_client_relevance``, used elsewhere for
this package's own qna artifact cache/state dirs)."""


def default_candidates(
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    site_dir: Path | None = None,
) -> tuple[Path, ...]:
    """The inventory search path, current directory first.

    Keyword overrides are the hermetic seam for tests: passing them skips the
    real ``Path.cwd()`` / ``Path.home()`` / ``platformdirs.site_config_dir()``
    calls entirely, so a test never touches the actual filesystem outside its
    own ``tmp_path``.
    """
    cwd = cwd if cwd is not None else Path.cwd()
    home = home if home is not None else Path.home()
    site_dir = (
        site_dir
        if site_dir is not None
        else Path(platformdirs.site_config_dir(SHARED_CONFIG_DIR_NAME))
    )
    return (
        cwd / DEFAULT_INVENTORY_FILENAME,
        home / f".{SHARED_CONFIG_DIR_NAME}" / DEFAULT_INVENTORY_FILENAME,
        site_dir / DEFAULT_INVENTORY_FILENAME,
    )


def find_inventory_path(candidates: Sequence[Path] | None = None) -> Path | None:
    """Return the first candidate that is a real file, or ``None``.

    ``is_file()``, not ``exists()``: a same-named directory must not be
    mistaken for the inventory.
    """
    search = default_candidates() if candidates is None else candidates

    for candidate in search:
        if candidate.is_file():
            logger.debug("found inventory at %s", candidate)
            return candidate

    logger.debug("no inventory found; searched %d candidate(s)", len(list(search)))
    return None


__all__ = [
    "DEFAULT_INVENTORY_FILENAME",
    "SHARED_CONFIG_DIR_NAME",
    "default_candidates",
    "find_inventory_path",
]
