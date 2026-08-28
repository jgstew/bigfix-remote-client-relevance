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

Extraction is pure Python so the controller needs no tooling of its own: the
``.deb`` outer format is a plain ``ar`` archive read here, its payload is
handled by stdlib :mod:`tarfile`, and ``.rpm`` goes through ``rpmfile``.

SSH keeps its in-target extraction, where a remote unpack does earn its keep.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath

from bigfix_remote_client_relevance.bootstrap.cache import default_cache_dir
from bigfix_remote_client_relevance.bootstrap.targets import MARKER_FILENAME
from bigfix_remote_client_relevance.results import ResolvedQna

logger = logging.getLogger(__name__)

AR_MAGIC = b"!<arch>\n"
_AR_HEADER_SIZE = 60

# One lock per destination, so a fan-out across many containers unpacks once.
# Process-local; the staging-then-rename below makes a cross-process race
# harmless rather than merely unlikely.
_locks: dict[str, asyncio.Lock] = {}


class LocalExtractionError(Exception):
    """An artifact could not be unpacked on the controller.

    Maps to ``error_kind="bootstrap"``.
    """


def extraction_destination(cache_dir: Path, qna: ResolvedQna) -> Path:
    """Where the extracted tree for this artifact lives.

    Keyed by the artifact's own ``<platform>-<arch>`` cache segment, so an
    extracted tree can never be confused with one from another platform.
    """
    if qna.artifact_path is None:
        raise LocalExtractionError(f"qna {qna.version} has no artifact to extract")
    return cache_dir / "extracted" / qna.version / qna.artifact_path.parent.name


async def ensure_extracted(qna: ResolvedQna, *, cache_dir: Path | None = None) -> Path:
    """Return the extracted qna tree, unpacking the artifact if needed."""
    cache_dir = cache_dir or default_cache_dir()
    destination = extraction_destination(cache_dir, qna)
    artifact = qna.artifact_path
    assert artifact is not None  # extraction_destination refuses otherwise

    lock = _locks.setdefault(str(destination), asyncio.Lock())
    async with lock:
        if (destination / MARKER_FILENAME).is_file():
            logger.debug("extraction cache hit: %s", destination)
            return destination

        # Unpack beside the target and rename on success, so neither a crash
        # nor a concurrent run ever observes a half-extracted tree.
        staging = destination.with_name(destination.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        logger.info("extracting %s into %s", artifact.name, destination)
        try:
            await asyncio.to_thread(_extract, artifact, staging)
            (staging / MARKER_FILENAME).write_text("", encoding="utf-8")
        except LocalExtractionError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise LocalExtractionError(f"could not extract {artifact.name}: {exc}") from exc

        staging.replace(destination)

    return destination


def _extract(artifact: Path, destination: Path) -> None:
    suffix = artifact.suffix.lower()
    if suffix == ".deb":
        _extract_deb(artifact, destination)
    elif suffix == ".rpm":
        _extract_rpm(artifact, destination)
    else:
        raise LocalExtractionError(
            f"cannot extract {suffix!r} artifacts on the controller "
            f"({artifact.name}); only .deb and .rpm are supported, which is "
            "every format a Linux container target can use"
        )


def _safe_destination(destination: Path, member_name: str) -> Path:
    """Resolve a member path, refusing anything that escapes the destination."""
    parts = [part for part in PurePosixPath(member_name).parts if part != "."]
    if PurePosixPath(member_name).is_absolute() or ".." in parts:
        raise LocalExtractionError(
            f"refusing to extract {member_name!r}: it points outside the destination"
        )
    if not parts:
        raise LocalExtractionError(f"refusing to extract {member_name!r}: empty member name")
    return destination.joinpath(*parts)


def _extract_deb(artifact: Path, destination: Path) -> None:
    payload = _ar_member(artifact, prefix="data.tar")
    with tarfile.open(fileobj=payload, mode="r:*") as tar:
        _extract_tar(tar, destination)


def _extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    for member in tar.getmembers():
        if member.isdir() and PurePosixPath(member.name).name in ("", "."):
            continue  # the archive's own root entry; destination already exists
        target = _safe_destination(destination, member.name)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            source = tar.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(stat.S_IMODE(member.mode))
        # Links and devices are deliberately skipped: nothing qna needs is one,
        # and following them is how an archive escapes its destination.


def _ar_member(artifact: Path, *, prefix: str) -> io.BytesIO:
    """Read the first `ar` member whose name starts with ``prefix``.

    The .deb outer format is an `ar` archive: an 8-byte magic, then per member
    a 60-byte ASCII header (16-byte name, then mtime/uid/gid/mode/size fields)
    followed by the body, padded to an even length.
    """
    with artifact.open("rb") as handle:
        if handle.read(len(AR_MAGIC)) != AR_MAGIC:
            raise LocalExtractionError(f"{artifact.name} is not an ar archive (bad magic)")
        while True:
            header = handle.read(_AR_HEADER_SIZE)
            if not header:
                raise LocalExtractionError(
                    f"{artifact.name} has no {prefix}* member; not a Debian package?"
                )
            if len(header) < _AR_HEADER_SIZE or not header.endswith(b"`\n"):
                raise LocalExtractionError(f"{artifact.name} has a malformed ar header")
            name = header[:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError as exc:
                raise LocalExtractionError(
                    f"{artifact.name} has an unreadable ar member size"
                ) from exc
            body = handle.read(size)
            if name.startswith(prefix):
                return io.BytesIO(body)
            if size % 2:
                handle.read(1)


def _extract_rpm(artifact: Path, destination: Path) -> None:
    # rpmfile handles the lead, the tag headers, and every payload compressor
    # in the wild — gzip on EL8-era packages, zstd on EL9-era ones. It does
    # not recognize the classic "lzma" tag SLE12-era SUSE packages use
    # (distinct from the "xz" tag it does handle) and silently falls back to
    # gzip, which then fails opaquely ("Not a gzipped file") deep inside cpio
    # parsing -- a real gap in rpmfile==2.2.1, not a corrupted download.
    # lzma.LZMAFile's default FORMAT_AUTO decodes both the "xz" container and
    # the legacy "lzma" stream identically -- the same call rpmfile already
    # makes for "xz" -- so pre-seeding its lazily-computed `data_file` cache
    # (`_fileobj`/`data_offset` are exactly what rpmfile's own property reads
    # to build that case) is enough; nothing about rpmfile itself changes.
    import lzma

    import rpmfile

    with rpmfile.open(str(artifact)) as archive:
        if archive.headers.get("archive_compression") == b"lzma":
            # Not a leaked handle: this becomes rpmfile's own cached
            # `data_file`, managed exactly like its gzip/zstd/bzip2
            # equivalents -- none of which rpmfile explicitly closes either;
            # `archive.__exit__` closes the underlying raw fileobj instead.
            archive._data_file = lzma.LZMAFile(  # noqa: SIM115
                rpmfile._SubFile(  # pyright: ignore[reportArgumentType]
                    archive._fileobj, archive.data_offset
                )
            )
        for member in archive.getmembers():
            # rpmfile reports permission bits only, with the file type split
            # out into these flags. Symlinks are skipped for the same reason
            # tar links are: nothing qna needs is one, and following them is
            # how an archive escapes its destination.
            if member.isdir or member.issymlink:
                continue
            target = _safe_destination(destination, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            with target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(stat.S_IMODE(member.mode))


__all__ = [
    "LocalExtractionError",
    "ensure_extracted",
    "extraction_destination",
]
