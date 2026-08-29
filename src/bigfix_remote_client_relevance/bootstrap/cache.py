"""Controller-side cache of downloaded qna artifacts.

Artifacts are immutable per full version, so each one is downloaded once,
verified against the release site's published checksum, and then reused
forever. Concurrent evaluations of the same (version, platform, arch) share a
single download rather than racing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import platformdirs

from bigfix_remote_client_relevance.bootstrap.release_site import ArtifactRef
from bigfix_remote_client_relevance.exceptions import BigFixRelevanceError
from bigfix_remote_client_relevance.results import ResolvedQna

logger = logging.getLogger(__name__)

APP_NAME = "bigfix_remote_client_relevance"

BytesFetcher = Callable[[str, Path], Awaitable[None]]

# One lock per cache key, so a fan-out across many hosts triggers at most one
# download per artifact. Process-local: two separate runs can still both
# download, which the atomic rename below makes harmless.
_locks: dict[str, asyncio.Lock] = {}


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

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Download beside the target and rename on success, so a crash or a
        # concurrent run never observes a half-written artifact.
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
    "ArtifactCacheError",
    "BytesFetcher",
    "artifact_destination",
    "default_cache_dir",
    "ensure_artifact",
]
