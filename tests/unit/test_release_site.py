"""Tests for resolving qna version specs against the BigFix release site.

Every test runs against captured HTML through an injected fetcher, so the
scraper is exercised offline and the recorded URLs can be asserted on.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.bootstrap.release_site import (
    RELEASE_INDEX_URL,
    ResolveError,
    artifact_for,
    resolve_version_spec,
)


class RecordingFetcher:
    """Serves captured pages by URL and records what was asked for."""

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages
        self.requested: list[str] = []

    def __call__(self, url: str) -> str:
        self.requested.append(url)
        try:
            return self._pages[url]
        except KeyError:
            raise ResolveError(f"no captured page for {url}") from None


@pytest.fixture
def pages(release_site_fixture):
    """URL -> captured page, mirroring the real site's layout."""
    return {
        RELEASE_INDEX_URL: release_site_fixture("release_index.html"),
        "https://support.bigfix.com/bes/release/11.0/patch6/": release_site_fixture(
            "patch_page_11.0.6.html"
        ),
        "https://support.bigfix.com/bes/release/11.0/patch6/SHA256SUMS": release_site_fixture(
            "SHA256SUMS.txt"
        ),
    }


@pytest.fixture
def fetch(pages):
    return RecordingFetcher(pages)


# --- version spec resolution ----------------------------------------------


def test_unset_spec_resolves_to_newest_agent_of_newest_stream(fetch):
    assert resolve_version_spec(None, fetch=fetch) == "11.0.6.137"


def test_stream_spec_resolves_to_newest_patch_in_that_stream(fetch):
    assert resolve_version_spec("11.0", fetch=fetch) == "11.0.6.137"


def test_older_stream_spec_resolves_within_its_own_stream(fetch):
    resolved = resolve_version_spec("10.0", fetch=fetch)

    assert resolved.startswith("10.0.")


def test_exact_spec_is_returned_without_any_fetch(fetch):
    """An exact version needs no index, so it works offline."""
    assert resolve_version_spec("11.0.6.137", fetch=fetch) == "11.0.6.137"
    assert fetch.requested == []


def test_agent_column_wins_over_release_column(fetch):
    """Patch 5 ships agent 11.0.5.204 while server/console/relay are .203."""
    resolved = resolve_version_spec("11.0", fetch=fetch, patch="patch5")

    assert resolved == "11.0.5.204"


def test_utilities_headings_are_not_treated_as_streams(fetch):
    """The index interleaves '11, 10 and 9.5 Utilities' tables with real streams."""
    with pytest.raises(ResolveError):
        resolve_version_spec("11, 10 and 9.5 Utilities", fetch=fetch)


def test_unknown_stream_raises_resolve_error_naming_the_url(fetch):
    with pytest.raises(ResolveError) as excinfo:
        resolve_version_spec("7.1", fetch=fetch)

    assert RELEASE_INDEX_URL in str(excinfo.value)


def test_reshaped_page_fails_loudly(fetch):
    broken = RecordingFetcher({RELEASE_INDEX_URL: "<html><body>redesigned</body></html>"})

    with pytest.raises(ResolveError) as excinfo:
        resolve_version_spec("11.0", fetch=broken)

    assert RELEASE_INDEX_URL in str(excinfo.value)


def test_malformed_spec_rejected(fetch):
    with pytest.raises(ResolveError):
        resolve_version_spec("not-a-version", fetch=fetch)


# --- artifact selection ----------------------------------------------------


def test_windows_uses_standalone_qna_zip_not_the_installer(fetch):
    """The BESAgent .exe is InstallShield and not practically extractable."""
    artifact = artifact_for("11.0.6.137", platform="windows", arch="x86_64", fetch=fetch)

    assert artifact.filename == "QNA11.0.6.137.zip"
    assert artifact.url.endswith("/util/QNA11.0.6.137.zip")
    assert ".exe" not in artifact.url


def test_macos_uses_the_pkg(fetch):
    artifact = artifact_for("11.0.6.137", platform="macos", arch="arm64", fetch=fetch)

    assert artifact.filename == "BESAgent-11.0.6.137-BigFix_MacOS11.0.pkg"


def test_ubuntu_uses_the_amd64_deb(fetch):
    artifact = artifact_for("11.0.6.137", platform="ubuntu", arch="x86_64", fetch=fetch)

    assert artifact.filename.endswith(".deb")
    assert "amd64" in artifact.filename


def test_rhel_uses_the_x86_64_rpm(fetch):
    artifact = artifact_for("11.0.6.137", platform="rhel", arch="x86_64", fetch=fetch)

    assert artifact.filename.endswith(".rpm")
    assert "x86_64" in artifact.filename


def test_rhel_arm64_uses_the_amazon_linux_named_rpm(fetch):
    """BigFix ships the rhel-family arm64 client under an Amazon Linux
    filename (`al2.aarch64.rpm`) -- officially supported only there, but a
    plain rpm that runs on any rhel-family arm64 host, so it belongs under
    the `rhel` platform rather than a separate one."""
    artifact = artifact_for("11.0.6.137", platform="rhel", arch="arm64", fetch=fetch)

    assert artifact.filename == "BESAgent-11.0.6.137-al2.aarch64.rpm"


# --- raspbian: one build, any requested arch maps onto it -----------------


@pytest.mark.parametrize("arch", ["armhf", "arm64", "aarch64", "x86_64"])
def test_raspbian_resolves_its_one_build_regardless_of_requested_arch(fetch, arch):
    """raspbian ships exactly one architecture -- unlike every other platform,
    an arbitrary requested arch must still map onto that build rather than
    fail to match a suffix the filename never carries."""
    artifact = artifact_for("11.0.6.137", platform="raspbian", arch=arch, fetch=fetch)

    assert artifact.filename == "BESAgent-11.0.6.137-raspbian10.armhf.deb"


# --- the raspbian armhf deb as an arm64 stand-in for debian/ubuntu ---------
#
# Neither publishes a native arm64 build. The raspbian armhf (32-bit ARM) deb
# is the only thing that runs on an arm64 host at all, via the kernel's
# 32-bit ARM userspace compat -- a cross-arch substitution, unlike the rhel
# case above where the filename genuinely is a 64-bit arm64 build.


def test_debian_arm64_falls_back_to_the_raspbian_armhf_deb(fetch):
    artifact = artifact_for("11.0.6.137", platform="debian", arch="arm64", fetch=fetch)

    assert artifact.filename == "BESAgent-11.0.6.137-raspbian10.armhf.deb"


def test_ubuntu_arm64_falls_back_to_the_raspbian_armhf_deb(fetch):
    artifact = artifact_for("11.0.6.137", platform="ubuntu", arch="arm64", fetch=fetch)

    assert artifact.filename == "BESAgent-11.0.6.137-raspbian10.armhf.deb"


def test_aarch64_spelling_also_triggers_the_raspbian_fallback(fetch):
    """The fallback is keyed off the normalized arch, not the literal string."""
    artifact = artifact_for("11.0.6.137", platform="ubuntu", arch="aarch64", fetch=fetch)

    assert artifact.filename == "BESAgent-11.0.6.137-raspbian10.armhf.deb"


def test_the_raspbian_fallback_never_leaks_into_an_x86_64_request(fetch):
    """The fallback pattern is a fixed literal with no {arch_deb} placeholder
    -- it must only be added as a candidate for arm64, or it would silently
    win an x86_64 lookup too if page order ever put it first."""
    artifact = artifact_for("11.0.6.137", platform="ubuntu", arch="x86_64", fetch=fetch)

    assert "raspbian" not in artifact.filename


def test_artifact_carries_published_sha256(fetch):
    artifact = artifact_for("11.0.6.137", platform="macos", arch="arm64", fetch=fetch)

    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.sha256 == artifact.sha256.lower()


def test_checksum_parsing_handles_filenames_containing_spaces(fetch):
    """SHA256SUMS is two-space separated; naive split() corrupts these names."""
    from bigfix_remote_client_relevance.bootstrap.release_site import parse_checksums

    sums = parse_checksums(fetch("https://support.bigfix.com/bes/release/11.0/patch6/SHA256SUMS"))

    assert "BES Client Compliance SDK 11.0.6.137.zip" in sums


def test_unsupported_platform_raises_resolve_error(fetch):
    with pytest.raises(ResolveError):
        artifact_for("11.0.6.137", platform="plan9", arch="x86_64", fetch=fetch)


def test_artifact_lookup_reports_the_url_it_tried(fetch):
    with pytest.raises(ResolveError) as excinfo:
        artifact_for("9.9.9.9", platform="macos", arch="arm64", fetch=fetch)

    assert "9.9" in str(excinfo.value)


# --- caching ---------------------------------------------------------------


def test_stream_resolution_is_cached_on_disk(fetch, tmp_path):
    first = resolve_version_spec("11.0", fetch=fetch, cache_dir=tmp_path)
    fetches_after_first = len(fetch.requested)
    second = resolve_version_spec("11.0", fetch=fetch, cache_dir=tmp_path)

    assert first == second
    assert len(fetch.requested) == fetches_after_first, "second resolve should hit the disk cache"


def test_refresh_forces_a_refetch(fetch, tmp_path):
    resolve_version_spec("11.0", fetch=fetch, cache_dir=tmp_path)
    before = len(fetch.requested)

    resolve_version_spec("11.0", fetch=fetch, cache_dir=tmp_path, refresh=True)

    assert len(fetch.requested) > before
