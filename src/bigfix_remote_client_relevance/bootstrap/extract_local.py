"""Unpack the qna artifact on the controller instead of inside the target.

Container targets used to extract the artifact themselves, which made
``dpkg-deb``/``ar``+``tar`` or ``rpm2cpio``+``cpio`` a prerequisite of every
image — tools that rockylinux:9 and amazonlinux:2023 do not ship, and whose
guest-side ``tar`` fails outright under amd64 emulation on Apple Silicon.

The container engine is local and the artifact is already on this disk, so
there is nothing to gain from unpacking on the far side. Extracting here once
per (version, platform, arch) and bind-mounting the tree removes the
prerequisite entirely, works on images with no package manager at all, and
pays the cost once for a whole fan-out rather than once per container.

SSH keeps its in-target extraction, where a remote unpack does earn its keep.
"""

from __future__ import annotations

from pathlib import Path

from bigfix_remote_client_relevance.results import ResolvedQna


class LocalExtractionError(Exception):
    """An artifact could not be unpacked on the controller.

    Maps to ``error_kind="bootstrap"``.
    """


def extraction_destination(cache_dir: Path, qna: ResolvedQna) -> Path:
    """Where the extracted tree for this artifact lives."""
    raise NotImplementedError


async def ensure_extracted(qna: ResolvedQna, *, cache_dir: Path | None = None) -> Path:
    """Return the extracted qna tree, unpacking the artifact if needed."""
    raise NotImplementedError


__all__ = [
    "LocalExtractionError",
    "ensure_extracted",
    "extraction_destination",
]
