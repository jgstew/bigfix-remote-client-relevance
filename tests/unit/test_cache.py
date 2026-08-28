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

    relative = resolved.artifact_path.relative_to(tmp_path)

    assert relative.parts[0] == "qna"
    assert "11.0.6.137" in relative.parts
    assert "windows-x86_64" in relative.parts
    assert relative.name == "QNA11.0.6.137.zip"


async def test_writes_sha256_sidecar(tmp_path):
    resolved = await ensure_artifact(
        "11.0.6.137", make_ref(), fetch_bytes=CountingFetcher(), cache_dir=tmp_path
    )

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

    assert str(default_cache_dir()).startswith("/tmp/fake-cache")
