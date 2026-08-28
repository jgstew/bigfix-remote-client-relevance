"""Tests for controller-side extraction of the qna artifact.

Containers used to unpack the artifact inside the target, which meant every
image had to ship dpkg-deb or ar+tar, or rpm2cpio+cpio — rockylinux:9 and
amazonlinux:2023 ship none of them, and guest tar breaks outright under amd64
emulation. The engine is local, so the artifact is unpacked here instead, once
per (version, platform, arch), and the tree is bind-mounted in.

The .deb fixtures are built in-test from stdlib pieces; the .rpm ones are real
rpms committed under tests/fixtures/artifacts (see its README).
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from bigfix_remote_client_relevance.bootstrap.extract_local import (
    LocalExtractionError,
    ensure_extracted,
    extraction_destination,
)
from bigfix_remote_client_relevance.bootstrap.targets import MARKER_FILENAME
from bigfix_remote_client_relevance.results import ResolvedQna

pytestmark = pytest.mark.xfail(
    strict=True, reason="M11: controller-side extraction not implemented"
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "artifacts"

QNA_MEMBER = "opt/BESClient/bin/qna"
QNA_BODY = b"#!/bin/sh\necho fixture qna\n"


def _data_tar(compression: str, members: dict[str, bytes] | None = None) -> bytes:
    """A deb payload tarball holding an executable qna."""
    members = members if members is not None else {QNA_MEMBER: QNA_BODY}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=f"w:{compression}") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(f"./{name}" if not name.startswith("./") else name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _ar_archive(entries: list[tuple[str, bytes]]) -> bytes:
    """A minimal `ar` container: the .deb outer format."""
    out = bytearray(b"!<arch>\n")
    for name, body in entries:
        out += f"{name:<16}{'0':<12}{'0':<6}{'0':<6}{'100644':<8}{len(body):<10}".encode()
        out += b"`\n" + body
        if len(body) % 2:
            out += b"\n"
    return bytes(out)


def write_deb(path: Path, compression: str = "xz", members: dict[str, bytes] | None = None) -> Path:
    payload = _data_tar(compression, members)
    suffix = {"xz": "xz", "gz": "gz"}[compression]
    path.write_bytes(
        _ar_archive(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", _data_tar("gz", {"control": b"Package: fixture\n"})),
                (f"data.tar.{suffix}", payload),
            ]
        )
    )
    return path


def resolved_for(artifact: Path, version: str = "11.0.6.137") -> ResolvedQna:
    return ResolvedQna(version=version, artifact_path=artifact)


@pytest.fixture
def cache(tmp_path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def deb(tmp_path) -> Path:
    artifact_dir = tmp_path / "artifacts" / "ubuntu-x86_64"
    artifact_dir.mkdir(parents=True)
    return write_deb(artifact_dir / "BESAgent-11.0.6.137-ubuntu18.amd64.deb")


def qna_in(tree: Path) -> Path:
    return tree / QNA_MEMBER


# --- deb ---------------------------------------------------------------------


async def test_deb_extracts_qna_tree(deb, cache):
    tree = await ensure_extracted(resolved_for(deb), cache_dir=cache)

    assert qna_in(tree).is_file()
    assert qna_in(tree).read_bytes() == QNA_BODY
    assert (tree / MARKER_FILENAME).is_file(), "a tree is usable only once marked complete"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
async def test_extracted_qna_is_executable(deb, cache):
    tree = await ensure_extracted(resolved_for(deb), cache_dir=cache)

    assert os.access(qna_in(tree), os.X_OK)


async def test_deb_gz_payload(tmp_path, cache):
    """Older builds ship data.tar.gz rather than data.tar.xz."""
    artifact = write_deb(tmp_path / "old.deb", compression="gz")

    tree = await ensure_extracted(resolved_for(artifact), cache_dir=cache)

    assert qna_in(tree).read_bytes() == QNA_BODY


async def test_tree_is_keyed_by_version_platform_and_arch(deb, cache):
    tree = await ensure_extracted(resolved_for(deb), cache_dir=cache)

    assert tree == extraction_destination(cache, resolved_for(deb))
    assert "11.0.6.137" in str(tree)
    assert "ubuntu-x86_64" in str(tree), "the artifact's platform-arch key must carry over"


# --- rpm ---------------------------------------------------------------------


@pytest.mark.parametrize("compressor", ["gzip", "zstd"])
async def test_rpm_extracts_qna_tree(compressor, cache):
    """EL8-era rpms use gzip payloads, EL9-era ones zstd; both are in the wild."""
    tree = await ensure_extracted(
        resolved_for(FIXTURES / f"tiny-qna-{compressor}.rpm"), cache_dir=cache
    )

    assert qna_in(tree).is_file()
    assert b"fixture qna" in qna_in(tree).read_bytes()
    assert (tree / MARKER_FILENAME).is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
async def test_rpm_preserves_the_executable_bit(cache):
    tree = await ensure_extracted(resolved_for(FIXTURES / "tiny-qna-zstd.rpm"), cache_dir=cache)

    assert os.access(qna_in(tree), os.X_OK)


# --- caching, atomicity, concurrency ------------------------------------------


async def test_second_call_reuses_the_tree(deb, cache):
    first = await ensure_extracted(resolved_for(deb), cache_dir=cache)
    qna_in(first).write_bytes(b"touched")

    second = await ensure_extracted(resolved_for(deb), cache_dir=cache)

    assert second == first
    assert qna_in(second).read_bytes() == b"touched", "a marked tree must not be re-extracted"


async def test_concurrent_calls_extract_once(deb, cache):
    trees = await asyncio.gather(
        *(ensure_extracted(resolved_for(deb), cache_dir=cache) for _ in range(4))
    )

    assert len({str(t) for t in trees}) == 1
    assert not list(cache.glob("**/*.partial")), "no staging directory may survive"


async def test_unmarked_tree_is_redone(deb, cache):
    tree = await ensure_extracted(resolved_for(deb), cache_dir=cache)
    (tree / MARKER_FILENAME).unlink()
    qna_in(tree).unlink()

    again = await ensure_extracted(resolved_for(deb), cache_dir=cache)

    assert qna_in(again).is_file(), "a half-extracted tree must be redone, not trusted"


async def test_failure_leaves_no_usable_tree(tmp_path, cache):
    corrupt = tmp_path / "corrupt.deb"
    corrupt.write_bytes(b"!<arch>\nnot really an archive")

    with pytest.raises(LocalExtractionError):
        await ensure_extracted(resolved_for(corrupt), cache_dir=cache)

    destination = extraction_destination(cache, resolved_for(corrupt))
    assert not destination.exists()
    assert not list(cache.glob("**/*.partial"))


async def test_retry_after_failure_succeeds(tmp_path, cache):
    artifact = tmp_path / "flaky.deb"
    artifact.write_bytes(b"!<arch>\ngarbage")
    with pytest.raises(LocalExtractionError):
        await ensure_extracted(resolved_for(artifact), cache_dir=cache)

    write_deb(artifact)
    tree = await ensure_extracted(resolved_for(artifact), cache_dir=cache)

    assert qna_in(tree).is_file()


# --- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["BESAgent.pkg", "QNA.zip"])
async def test_unsupported_format_raises(tmp_path, cache, name):
    """macOS and Windows are SSH-only; containers are Linux, so deb/rpm is all."""
    artifact = tmp_path / name
    artifact.write_bytes(b"whatever")

    with pytest.raises(LocalExtractionError) as excinfo:
        await ensure_extracted(resolved_for(artifact), cache_dir=cache)

    assert artifact.suffix in str(excinfo.value)


async def test_rejects_path_traversal_members(tmp_path, cache):
    escape = tmp_path / "escapee"
    artifact = write_deb(
        tmp_path / "evil.deb",
        members={QNA_MEMBER: QNA_BODY, f"../../{escape.name}": b"pwned"},
    )

    with pytest.raises(LocalExtractionError):
        await ensure_extracted(resolved_for(artifact), cache_dir=cache)

    assert not escape.exists(), "extraction must never write outside the destination"
