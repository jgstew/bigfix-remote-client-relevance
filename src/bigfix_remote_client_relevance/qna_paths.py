"""Locating a qna binary.

Candidate lists are ported from ``jgstew/EvaluateRelevance``'s ``get_path_qna()``,
with the bare ``qna`` / ``qna.exe`` entries replaced by a real ``$PATH`` lookup —
the original checked those against the current directory, which only worked by
accident.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Sequence

logger = logging.getLogger(__name__)

MACOS_CANDIDATES: tuple[str, ...] = (
    "/usr/local/bin/qna",
    "/Library/BESAgent/BESAgent.app/Contents/MacOS/QnA",
    "/opt/BESClient/bin/qna",
)

LINUX_CANDIDATES: tuple[str, ...] = (
    "/usr/local/bin/qna",
    "/opt/BESClient/bin/qna",
)

WINDOWS_CANDIDATES: tuple[str, ...] = (
    r"C:/Program Files (x86)/BigFix Enterprise/BES Client/qna.exe",
    r"C:/Program Files/BigFix Enterprise/BES Client/qna.exe",
)

_PATH_NAMES: tuple[str, ...] = ("qna", "QnA", "qna.exe")


def default_candidates(platform: str | None = None) -> tuple[str, ...]:
    """Filesystem locations where a BES client installs qna, in search order."""
    platform = platform if platform is not None else sys.platform
    if platform == "darwin":
        return MACOS_CANDIDATES
    if platform.startswith("win"):
        return WINDOWS_CANDIDATES
    return LINUX_CANDIDATES


def find_qna_path(candidates: Sequence[str] | None = None) -> str | None:
    """Return the first usable qna binary, or None if there is none.

    Falls back to ``$PATH`` after the platform candidates are exhausted.
    """
    search = default_candidates() if candidates is None else candidates

    for candidate in search:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            logger.debug("found qna at candidate path %s", candidate)
            return candidate

    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            logger.debug("found qna on PATH as %s -> %s", name, found)
            return found

    logger.debug("no qna binary found; searched %d candidates and PATH", len(list(search)))
    return None
