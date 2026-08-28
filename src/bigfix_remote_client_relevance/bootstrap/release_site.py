"""Resolve qna version specs and artifact URLs against support.bigfix.com.

The release site is the source of truth for what versions exist and where their
artifacts live. Its HTML has no stability contract, so this module keys off the
few things that are semantically meaningful (heading text, table header names,
the stable ``h3`` ids on patch pages) and raises :class:`ResolveError` naming
the URL it tried whenever the shape is not what it expects — a loud failure is
much easier to diagnose than silently resolving zero artifacts.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

RELEASE_INDEX_URL = "https://support.bigfix.com/bes/release/"

Fetcher = Callable[[str], str]

# Streams are "11.0"/"9.5"; full versions are four-part like "11.0.6.137".
_STREAM = re.compile(r"^\d+\.\d+$")
_FULL_VERSION = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_PATCH_HREF = re.compile(r"^(\d+\.\d+)/patch(\d+)/?$")

# How long a stream -> newest-patch lookup stays fresh. Exact versions never
# consult this, so pinned runs work offline once the artifact is cached.
STREAM_CACHE_TTL_S = 24 * 60 * 60

# Selects the agent artifact for a target. Windows is the one platform with a
# standalone QnA download; everywhere else qna is extracted out of the agent
# package without installing it.
_PLATFORM_PATTERNS: dict[str, tuple[str, ...]] = {
    "windows": (r"^QNA[\d.]+\.zip$",),
    "macos": (r"^BESAgent-[\d.]+-BigFix_MacOS.*\.pkg$",),
    "ubuntu": (r"^BESAgent-[\d.]+-ubuntu\d+\.{arch_deb}\.deb$",),
    "debian": (r"^BESAgent-[\d.]+-debian\d+\.{arch_deb}\.deb$",),
    "raspbian": (r"^BESAgent-[\d.]+-raspbian\d+\.{arch_deb}\.deb$",),
    "rhel": (r"^BESAgent-[\d.]+-rhe\d+\.{arch_rpm}\.rpm$",),
    "suse": (r"^BESAgent-[\d.]+-sle\d+\.{arch_rpm}\.rpm$",),
    "amazonlinux": (r"^BESAgent-[\d.]+-al\d+\.{arch_rpm}\.rpm$",),
}

# Debian and rpm families spell the same architectures differently.
_ARCH_DEB = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
_ARCH_RPM = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}


class ResolveError(Exception):
    """A version spec or artifact could not be resolved.

    Maps to ``error_kind="resolve"``.
    """


@dataclass(frozen=True)
class ArtifactRef:
    """A downloadable qna-bearing artifact for one platform/arch."""

    url: str
    filename: str
    sha256: str
    platform: str
    arch: str


def _default_fetch(url: str) -> str:
    import requests

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ResolveError(f"could not fetch {url}: {exc}") from exc
    return response.text


def _soup(html: str, url: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    if soup.find("table") is None:
        raise ResolveError(
            f"no tables found at {url}; the release site layout may have changed"
        )
    return soup


def _attr(tag: Tag, name: str) -> str | None:
    """Read an HTML attribute as a string.

    bs4 types attributes as ``str | list[str] | None`` because some are
    multi-valued; the ones read here never are.
    """
    value = tag.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return value[0]
    return None


def _table_headers(table: Tag) -> list[str]:
    header_row = table.find("tr")
    if not isinstance(header_row, Tag):
        return []
    return [cell.get_text(strip=True) for cell in header_row.find_all(["th", "td"])]


def parse_checksums(text: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` file into ``{filename: digest}``.

    The format separates digest from filename with exactly two spaces, and some
    published filenames contain spaces themselves, so splitting on whitespace
    would truncate them.
    """
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if "  " not in line:
            continue
        digest, _, filename = line.partition("  ")
        digest = digest.strip()
        if digest:
            sums[filename.strip()] = digest.lower()
    return sums


def _stream_tables(soup: BeautifulSoup) -> dict[str, Tag]:
    """Map stream name ("11.0") to its release table.

    Index headings are ``<h2>`` whose ids are meaningless (``section``,
    ``section-1``, ...), so the heading *text* is the key. Utilities tables are
    interleaved with a different schema and are skipped.
    """
    tables: dict[str, Tag] = {}
    for heading in soup.find_all("h2"):
        label = heading.get_text(strip=True)
        if "utilit" in label.lower():
            continue
        table = heading.find_next("table")
        if not isinstance(table, Tag):
            continue
        headers = _table_headers(table)
        if "Agent" not in headers:
            continue
        # Headings are bare stream numbers: "11", "10", "9.5".
        stream = label if "." in label else f"{label}.0"
        if _STREAM.match(stream):
            tables[stream] = table
    return tables


def _agent_versions(table: Tag, headers: list[str]) -> list[tuple[str, str]]:
    """Return ``(patch_slug, agent_version)`` newest-first for one stream table.

    The Agent column is read specifically: it is the version qna ships with, and
    it can differ from the Server/Console/Relay columns in the same row.
    """
    agent_index = headers.index("Agent")
    rows: list[tuple[str, str]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) <= agent_index:
            continue
        link = cells[0].find("a")
        href = _attr(link, "href") if isinstance(link, Tag) else None
        if href is None:
            continue
        match = _PATCH_HREF.match(href.strip())
        if match is None:
            continue
        version = cells[agent_index].get_text(strip=True)
        if not _FULL_VERSION.match(version):
            # Older patches list "N/A" where no agent shipped.
            continue
        rows.append((f"patch{match.group(2)}", version))
    return rows


def _cache_path(cache_dir: Path) -> Path:
    return cache_dir / "release_index.json"


def _read_stream_cache(cache_dir: Path | None, refresh: bool) -> dict[str, str] | None:
    if cache_dir is None or refresh:
        return None
    path = _cache_path(cache_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - payload.get("fetched_at", 0) > STREAM_CACHE_TTL_S:
        logger.debug("stream cache expired")
        return None
    streams = payload.get("streams")
    return streams if isinstance(streams, dict) else None


def _write_stream_cache(cache_dir: Path | None, streams: dict[str, str]) -> None:
    if cache_dir is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(cache_dir).write_text(
        json.dumps({"fetched_at": time.time(), "streams": streams}), encoding="utf-8"
    )


def resolve_version_spec(
    spec: str | None,
    *,
    fetch: Fetcher | None = None,
    cache_dir: Path | None = None,
    refresh: bool = False,
    patch: str | None = None,
) -> str:
    """Resolve a version spec to a full four-part agent version.

    Args:
        spec: ``None`` for the newest agent overall, a stream like ``"11.0"``,
            or an exact version like ``"11.0.6.137"`` (returned as-is, no fetch).
        patch: Pin a specific patch slug (``"patch5"``) within the stream.

    Two runs of the same stream spec on different days may resolve differently;
    that is the point, which is why results always record the resolved version
    rather than the spec.
    """
    if spec and _FULL_VERSION.match(spec):
        return spec

    if spec is not None and not _STREAM.match(spec) and not _STREAM.match(f"{spec}.0"):
        raise ResolveError(
            f"{spec!r} is not a version spec; expected a stream like '11.0', "
            f"a full version like '11.0.6.137', or None (index: {RELEASE_INDEX_URL})"
        )

    normalized = None if spec is None else (spec if "." in spec else f"{spec}.0")

    if patch is None and normalized is not None:
        cached = _read_stream_cache(cache_dir, refresh)
        if cached and normalized in cached:
            logger.debug("stream %s resolved from cache to %s", normalized, cached[normalized])
            return cached[normalized]

    fetch = fetch or _default_fetch
    soup = _soup(fetch(RELEASE_INDEX_URL), RELEASE_INDEX_URL)
    tables = _stream_tables(soup)
    if not tables:
        raise ResolveError(
            f"no release stream tables found at {RELEASE_INDEX_URL}; layout may have changed"
        )

    newest_by_stream: dict[str, str] = {}
    for stream, table in tables.items():
        rows = _agent_versions(table, _table_headers(table))
        if rows:
            newest_by_stream[stream] = rows[0][1]
    _write_stream_cache(cache_dir, newest_by_stream)

    if normalized is None:
        # The index lists streams newest-first.
        first = next(iter(tables))
        resolved = newest_by_stream.get(first)
        if resolved is None:
            raise ResolveError(f"no agent versions listed at {RELEASE_INDEX_URL}")
        return resolved

    if normalized not in tables:
        available = ", ".join(sorted(tables)) or "none"
        raise ResolveError(
            f"no release stream {normalized!r} at {RELEASE_INDEX_URL} (available: {available})"
        )

    rows = _agent_versions(tables[normalized], _table_headers(tables[normalized]))
    if not rows:
        raise ResolveError(f"stream {normalized!r} lists no agent versions at {RELEASE_INDEX_URL}")

    if patch is not None:
        for slug, version in rows:
            if slug == patch:
                return version
        raise ResolveError(f"{patch!r} not found in stream {normalized!r} at {RELEASE_INDEX_URL}")

    return rows[0][1]


def _patch_page_url(full_version: str) -> str:
    major, minor, patch, _build = full_version.split(".")
    return urljoin(RELEASE_INDEX_URL, f"{major}.{minor}/patch{patch}/")


def _candidate_links(soup: BeautifulSoup, page_url: str) -> dict[str, str]:
    """Every download link on a patch page, as ``{filename: absolute url}``.

    Many rows share one href (a single rpm serves the whole RHEL family), so
    this deduplicates by filename.
    """
    links: dict[str, str] = {}
    for anchor in soup.find_all("a"):
        href = _attr(anchor, "href") if isinstance(anchor, Tag) else None
        if not href:
            continue
        absolute = urljoin(page_url, href)
        # Filenames are URL-encoded in hrefs (the SDK zip contains spaces).
        filename = unquote(Path(urlparse(absolute).path).name)
        if filename:
            links[filename] = absolute
    return links


def artifact_for(
    full_version: str,
    *,
    platform: str,
    arch: str,
    fetch: Fetcher | None = None,
) -> ArtifactRef:
    """Find the artifact carrying qna for ``platform``/``arch`` at ``full_version``."""
    if not _FULL_VERSION.match(full_version):
        raise ResolveError(f"artifact lookup needs a full version, got {full_version!r}")

    patterns = _PLATFORM_PATTERNS.get(platform)
    if patterns is None:
        supported = ", ".join(sorted(_PLATFORM_PATTERNS))
        raise ResolveError(f"unsupported platform {platform!r}; known: {supported}")

    page_url = _patch_page_url(full_version)
    fetch = fetch or _default_fetch
    soup = _soup(fetch(page_url), page_url)
    links = _candidate_links(soup, page_url)

    compiled = [
        re.compile(
            p.format(
                arch_deb=_ARCH_DEB.get(arch, re.escape(arch)),
                arch_rpm=_ARCH_RPM.get(arch, re.escape(arch)),
            )
        )
        for p in patterns
    ]

    for filename, url in links.items():
        if full_version not in filename:
            continue
        if any(pattern.match(filename) for pattern in compiled):
            checksums = _checksums_for(page_url, fetch)
            return ArtifactRef(
                url=url,
                filename=filename,
                sha256=checksums.get(filename, ""),
                platform=platform,
                arch=arch,
            )

    raise ResolveError(
        f"no {platform}/{arch} qna artifact for {full_version} at {page_url}"
    )


def _checksums_for(page_url: str, fetch: Fetcher) -> dict[str, str]:
    """Fetch the patch page's SHA256SUMS; absence is not fatal here.

    The cache layer decides what to do about an artifact with no published
    digest — it refuses to store one, which keeps that policy in one place.
    """
    url = urljoin(page_url, "SHA256SUMS")
    try:
        return parse_checksums(fetch(url))
    except ResolveError:
        logger.warning("no published checksums at %s", url)
        return {}


__all__ = [
    "RELEASE_INDEX_URL",
    "ArtifactRef",
    "Fetcher",
    "ResolveError",
    "artifact_for",
    "parse_checksums",
    "resolve_version_spec",
]
