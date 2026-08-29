"""Controller-side cache of downloaded qna artifacts.

Artifacts are immutable per full version, so each one is downloaded once,
verified against the release site's published checksum, and then reused
forever. Concurrent evaluations of the same (version, platform, arch) share a
single download rather than racing -- both within one process (an
``asyncio.Lock`` per cache key) and across processes (a ``filelock.FileLock``
sibling to the artifact), so several MCP server processes sharing one
machine's cache pay for one download between them, not one each.

Correctness never depended on either lock: the staging-file + checksum +
atomic-rename sequence means a race can never serve a partial or corrupt
artifact. Both locks exist purely so a race does not also waste a download.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import filelock
import platformdirs

from bigfix_remote_client_relevance.bootstrap.release_site import ArtifactRef
from bigfix_remote_client_relevance.exceptions import BigFixRelevanceError
from bigfix_remote_client_relevance.results import ResolvedQna

logger = logging.getLogger(__name__)

APP_NAME = "bigfix_remote_client_relevance"

BytesFetcher = Callable[[str, Path], Awaitable[None]]

# One lock per cache key, so a fan-out across many hosts triggers at most one
# in-process task talking to the cross-process lock per artifact.
_locks: dict[str, asyncio.Lock] = {}

DEFAULT_LOCK_TIMEOUT_S = 600.0
"""How long a waiter blocks on a live holder before giving up.

Not a stale-lock heuristic: ``filelock`` uses the OS's own lock primitive
(``flock``/``LockFile``), which the kernel releases the instant a holding
process exits, crash or not -- there is no orphaned lock file to detect or
break. This timeout only bounds how long a *live* wait can run, for a
genuinely wedged download or a filesystem where OS-level locking is
unreliable. 10 minutes is generous relative to a qna artifact's size.
"""


@contextlib.asynccontextmanager
async def _cross_process_lock(path: Path, *, timeout_s: float) -> AsyncIterator[None]:
    """Hold an OS-level lock on ``path`` for the duration of the block.

    The lock file is a sibling of ``path`` -- ``path`` itself is never
    created, opened, or touched by this function. ``filelock``'s calls are
    blocking, so both acquire and release run off the event loop.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f"{path.name}.lock"
    # thread_local=False: acquire() and release() below each run in a
    # separate asyncio.to_thread worker thread, not necessarily the same one.
    # filelock's default (thread_local=True) keeps its open fd and lock
    # counter in thread-local storage, so release() on a different thread
    # from acquire() would silently see no lock to release, leaking the fd.
    lock = filelock.FileLock(str(lock_path), timeout=timeout_s, thread_local=False)

    try:
        await asyncio.to_thread(lock.acquire, timeout=0)
    except filelock.Timeout:
        logger.info(
            "waiting up to %.0fs for the artifact cache lock at %s "
            "(likely held by another process downloading the same artifact)",
            timeout_s,
            lock_path,
        )
        try:
            await asyncio.to_thread(lock.acquire)
        except filelock.Timeout as exc:
            raise ArtifactCacheError(
                f"timed out after {timeout_s:.0f}s waiting for the artifact cache lock "
                f"at {lock_path}; check for a stuck process holding it"
            ) from exc

    try:
        yield
    finally:
        lock.release()


class ArtifactCacheError(BigFixRelevanceError):
    """An artifact could not be downloaded or verified.

    Maps to ``error_kind="bootstrap"``.
    """


def default_cache_dir() -> Path:
    """Platform user-cache directory, e.g. ``~/Library/Caches/...`` on macOS.

    Safe to delete: everything here is re-downloadable.
    """
    return Path(platformdirs.user_cache_dir(APP_NAME))


def artifact_destination(cache_dir: Path, version: str, ref: ArtifactRef) -> Path:
    return cache_dir / "qna" / version / f"{ref.platform}-{ref.arch}" / ref.filename


async def _default_fetch_bytes(url: str, destination: Path) -> None:
    import requests

    def _download() -> None:
        with requests.get(url, timeout=300, stream=True) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    handle.write(chunk)

    try:
        await asyncio.to_thread(_download)
    except requests.RequestException as exc:
        raise ArtifactCacheError(f"could not download {url}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def ensure_artifact(
    version: str,
    ref: ArtifactRef,
    *,
    fetch_bytes: BytesFetcher | None = None,
    cache_dir: Path | None = None,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> ResolvedQna:
    """Return a cached, checksum-verified artifact, downloading it if needed."""
    cache_dir = cache_dir or default_cache_dir()
    destination = artifact_destination(cache_dir, version, ref)
    key = str(destination)

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if destination.is_file():
            logger.debug("artifact cache hit: %s", destination)
            return ResolvedQna(version=version, artifact_path=destination)

        if not ref.sha256:
            raise ArtifactCacheError(
                f"no published sha256 for {ref.filename} at {ref.url}; refusing to "
                "cache an artifact that cannot be verified"
            )

        async with _cross_process_lock(destination, timeout_s=lock_timeout_s):
            # A sibling process may have finished the download while we were
            # waiting for the lock above -- the in-process check at the top of
            # this function only ever saw our own process's view.
            if destination.is_file():
                logger.debug("artifact cache hit after waiting for lock: %s", destination)
                return ResolvedQna(version=version, artifact_path=destination)

            destination.parent.mkdir(parents=True, exist_ok=True)
            # Download beside the target and rename on success, so a crash or
            # a concurrent run never observes a half-written artifact.
            staging = destination.with_name(destination.name + ".part")
            fetch_bytes = fetch_bytes or _default_fetch_bytes

            logger.info("downloading qna artifact %s (%s)", ref.filename, version)
            try:
                await fetch_bytes(ref.url, staging)
            except ArtifactCacheError:
                staging.unlink(missing_ok=True)
                raise
            except Exception as exc:
                staging.unlink(missing_ok=True)
                raise ArtifactCacheError(f"could not download {ref.url}: {exc}") from exc

            actual = _sha256(staging)
            if actual != ref.sha256.lower():
                staging.unlink(missing_ok=True)
                raise ArtifactCacheError(
                    f"checksum mismatch for {ref.filename} from {ref.url}: "
                    f"expected {ref.sha256.lower()}, got {actual}"
                )

            staging.replace(destination)
            destination.with_name(destination.name + ".sha256").write_text(
                f"{actual}  {ref.filename}\n", encoding="utf-8"
            )
            logger.info("cached %s at %s", ref.filename, destination)

    return ResolvedQna(version=version, artifact_path=destination)


__all__ = [
    "APP_NAME",
    "DEFAULT_LOCK_TIMEOUT_S",
    "ArtifactCacheError",
    "BytesFetcher",
    "artifact_destination",
    "default_cache_dir",
    "ensure_artifact",
]
