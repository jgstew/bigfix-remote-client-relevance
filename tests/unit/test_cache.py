"""Tests for the controller-side qna artifact cache.

The controller downloads each artifact once and pushes it to targets, so a
10-host fan-out costs one download rather than ten.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from bigfix_remote_client_relevance.bootstrap.cache import (
    ArtifactCacheError,
    _cross_process_lock,
    default_cache_dir,
    ensure_artifact,
)
from bigfix_remote_client_relevance.bootstrap.release_site import ArtifactRef

PAYLOAD = b"pretend this is a qna artifact"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def make_ref(sha256: str = DIGEST) -> ArtifactRef:
    return ArtifactRef(
        url="https://software.bigfix.com/download/bes/110/util/QNA11.0.6.137.zip",
        filename="QNA11.0.6.137.zip",
        sha256=sha256,
        platform="windows",
        arch="x86_64",
    )


class CountingFetcher:
    """Writes a fixed payload to the destination and counts calls."""

    def __init__(self, payload: bytes = PAYLOAD, delay: float = 0.0) -> None:
        self.payload = payload
        self.delay = delay
        self.calls = 0

    async def __call__(self, url: str, destination: Path) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        destination.write_bytes(self.payload)


async def test_downloads_and_returns_resolved_qna(tmp_path):
    fetcher = CountingFetcher()

    resolved = await ensure_artifact(
        "11.0.6.137", make_ref(), fetch_bytes=fetcher, cache_dir=tmp_path
    )

    assert resolved.version == "11.0.6.137"
    assert resolved.artifact_path is not None
    assert resolved.artifact_path.read_bytes() == PAYLOAD
    assert fetcher.calls == 1


async def test_cache_layout_is_version_platform_artifact(tmp_path):
    resolved = await ensure_artifact(
        "11.0.6.137", make_ref(), fetch_bytes=CountingFetcher(), cache_dir=tmp_path
    )

    assert resolved.artifact_path is not None
    relative = resolved.artifact_path.relative_to(tmp_path)

    assert relative.parts[0] == "qna"
    assert "11.0.6.137" in relative.parts
    assert "windows-x86_64" in relative.parts
    assert relative.name == "QNA11.0.6.137.zip"


async def test_writes_sha256_sidecar(tmp_path):
    resolved = await ensure_artifact(
        "11.0.6.137", make_ref(), fetch_bytes=CountingFetcher(), cache_dir=tmp_path
    )

    assert resolved.artifact_path is not None
    sidecar = resolved.artifact_path.with_suffix(resolved.artifact_path.suffix + ".sha256")

    assert sidecar.read_text(encoding="utf-8").strip().startswith(DIGEST)


async def test_second_call_reuses_the_cached_artifact(tmp_path):
    fetcher = CountingFetcher()
    await ensure_artifact("11.0.6.137", make_ref(), fetch_bytes=fetcher, cache_dir=tmp_path)

    await ensure_artifact("11.0.6.137", make_ref(), fetch_bytes=fetcher, cache_dir=tmp_path)

    assert fetcher.calls == 1, "artifacts are immutable per version; do not refetch"


async def test_checksum_mismatch_raises_and_leaves_no_artifact(tmp_path):
    fetcher = CountingFetcher(payload=b"corrupted in transit")

    with pytest.raises(ArtifactCacheError) as excinfo:
        await ensure_artifact("11.0.6.137", make_ref(), fetch_bytes=fetcher, cache_dir=tmp_path)

    assert DIGEST in str(excinfo.value)
    assert not list(tmp_path.rglob("QNA11.0.6.137.zip")), "corrupt download must not be cached"


async def test_failed_download_leaves_no_partial_file(tmp_path):
    async def exploding(url: str, destination: Path) -> None:
        destination.write_bytes(b"half")
        raise OSError("connection reset")

    with pytest.raises(ArtifactCacheError):
        await ensure_artifact("11.0.6.137", make_ref(), fetch_bytes=exploding, cache_dir=tmp_path)

    assert not list(tmp_path.rglob("QNA11.0.6.137.zip"))


async def test_concurrent_requests_download_once(tmp_path):
    """A 10-host fan-out must not trigger 10 downloads of the same artifact."""
    fetcher = CountingFetcher(delay=0.05)

    results = await asyncio.gather(
        *[
            ensure_artifact("11.0.6.137", make_ref(), fetch_bytes=fetcher, cache_dir=tmp_path)
            for _ in range(10)
        ]
    )

    assert fetcher.calls == 1
    assert {r.artifact_path for r in results} == {results[0].artifact_path}


async def test_distinct_artifacts_download_independently(tmp_path):
    fetcher = CountingFetcher()
    windows = make_ref()
    macos = ArtifactRef(
        url="https://software.bigfix.com/download/bes/110/BESAgent-11.0.6.137-BigFix_MacOS11.0.pkg",
        filename="BESAgent-11.0.6.137-BigFix_MacOS11.0.pkg",
        sha256=DIGEST,
        platform="macos",
        arch="arm64",
    )

    await ensure_artifact("11.0.6.137", windows, fetch_bytes=fetcher, cache_dir=tmp_path)
    await ensure_artifact("11.0.6.137", macos, fetch_bytes=fetcher, cache_dir=tmp_path)

    assert fetcher.calls == 2


async def test_unverifiable_artifact_is_rejected(tmp_path):
    """A missing published checksum must not silently skip verification."""
    with pytest.raises(ArtifactCacheError):
        await ensure_artifact(
            "11.0.6.137",
            make_ref(sha256=""),
            fetch_bytes=CountingFetcher(),
            cache_dir=tmp_path,
        )


def test_default_cache_dir_is_under_the_user_cache(monkeypatch):
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_cache_dir", lambda *a, **k: "/tmp/fake-cache")

    # Compared as paths: str() of a Path renders with the platform separator.
    assert default_cache_dir() == Path("/tmp/fake-cache")


# --- the cross-process lock -------------------------------------------------
#
# Several MCP server processes can share one machine's cache dir. The
# staging-file + checksum + atomic-rename sequence already makes a race
# harmless; this is what stops it being *wasteful* -- two processes should
# never both download the same artifact.
#
# These test `_cross_process_lock` directly rather than through
# `ensure_artifact`, because `ensure_artifact` also holds an in-process
# `asyncio.Lock` first -- going through it would only prove the in-process
# lock works, which test_concurrent_requests_download_once already covers.


async def test_second_acquirer_waits_for_the_first_to_release(tmp_path):
    path = tmp_path / "artifact.bin"
    events: list[str] = []

    async def holder():
        async with _cross_process_lock(path, timeout_s=5.0):
            events.append("holder-acquired")
            await asyncio.sleep(0.1)
            events.append("holder-released")

    async def waiter():
        await asyncio.sleep(0.02)  # let the holder acquire first
        async with _cross_process_lock(path, timeout_s=5.0):
            events.append("waiter-acquired")

    await asyncio.gather(holder(), waiter())

    assert events == ["holder-acquired", "holder-released", "waiter-acquired"]


async def test_timeout_raises_artifact_cache_error_not_a_bare_filelock_error(tmp_path):
    path = tmp_path / "artifact.bin"
    holder_has_lock = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder():
        async with _cross_process_lock(path, timeout_s=5.0):
            holder_has_lock.set()
            await release_holder.wait()

    holder_task = asyncio.create_task(holder())
    await holder_has_lock.wait()
    try:
        with pytest.raises(ArtifactCacheError, match="timed out"):
            async with _cross_process_lock(path, timeout_s=0.05):
                pass
    finally:
        release_holder.set()
        await holder_task


async def test_lock_releases_even_when_the_body_raises(tmp_path):
    path = tmp_path / "artifact.bin"

    with pytest.raises(ValueError, match="boom"):
        async with _cross_process_lock(path, timeout_s=5.0):
            raise ValueError("boom")

    # If the first lock leaked, this would hang and the test would time out.
    async with _cross_process_lock(path, timeout_s=5.0):
        pass


async def test_creates_the_parent_directory_if_missing(tmp_path):
    path = tmp_path / "nested" / "deeper" / "artifact.bin"

    async with _cross_process_lock(path, timeout_s=5.0):
        assert path.parent.is_dir()


async def test_lock_file_is_a_sibling_named_after_the_artifact(tmp_path):
    path = tmp_path / "artifact.bin"

    async with _cross_process_lock(path, timeout_s=5.0):
        assert (tmp_path / "artifact.bin.lock").exists()


async def test_two_processes_download_the_artifact_once(tmp_path):
    """The load-bearing test: proves the dedup crosses a real process boundary,
    not just an asyncio event loop."""
    import sys
    import textwrap

    marker_dir = tmp_path / "fetch-markers"
    marker_dir.mkdir()

    script = textwrap.dedent(
        f"""
        import asyncio, hashlib, time, uuid
        from pathlib import Path
        from bigfix_remote_client_relevance.bootstrap.cache import ensure_artifact
        from bigfix_remote_client_relevance.bootstrap.release_site import ArtifactRef

        payload = {PAYLOAD!r}
        digest = hashlib.sha256(payload).hexdigest()
        ref = ArtifactRef(
            url="https://software.bigfix.com/download/bes/110/util/QNA11.0.6.137.zip",
            filename="QNA11.0.6.137.zip",
            sha256=digest,
            platform="windows",
            arch="x86_64",
        )

        async def fetch_bytes(url, destination):
            (Path({str(marker_dir)!r}) / str(uuid.uuid4())).write_text("fetched")
            await asyncio.sleep(0.3)
            destination.write_bytes(payload)

        asyncio.run(
            ensure_artifact(
                "11.0.6.137", ref, fetch_bytes=fetch_bytes, cache_dir=Path({str(tmp_path)!r})
            )
        )
        """
    )

    procs = [await asyncio.create_subprocess_exec(sys.executable, "-c", script) for _ in range(2)]
    for proc in procs:
        assert await asyncio.wait_for(proc.wait(), timeout=30) == 0

    assert len(list(marker_dir.iterdir())) == 1
