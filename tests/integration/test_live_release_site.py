"""Smoke checks against the live BigFix release site.

The site has no stability contract, so these guard against a layout change that
the captured fixtures cannot detect. Opt in with::

    BFRCR_NETWORK_TESTS=1 uv run pytest -m network

No agent packages are downloaded here; artifact URLs are only HEAD-checked.
"""

from __future__ import annotations

import re

import pytest

from bigfix_remote_client_relevance.bootstrap.release_site import (
    artifact_for,
    resolve_version_spec,
)

pytestmark = pytest.mark.network

FULL_VERSION = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def test_newest_version_resolves():
    resolved = resolve_version_spec(None)

    assert FULL_VERSION.match(resolved)


def test_stream_11_0_resolves_within_its_stream():
    resolved = resolve_version_spec("11.0")

    assert resolved.startswith("11.0.")


@pytest.mark.parametrize(
    ("platform", "arch"),
    [
        ("windows", "x86_64"),
        ("macos", "arm64"),
        ("ubuntu", "x86_64"),
        ("ubuntu", "arm64"),
        ("debian", "arm64"),
        ("rhel", "x86_64"),
        ("rhel", "arm64"),
    ],
)
def test_artifact_urls_are_live(platform, arch):
    import requests

    version = resolve_version_spec("11.0")
    artifact = artifact_for(version, platform=platform, arch=arch)

    assert artifact.sha256, "the release site should publish a checksum"

    response = requests.head(artifact.url, timeout=30, allow_redirects=True)
    assert response.status_code == 200, f"{artifact.url} returned {response.status_code}"


@pytest.mark.parametrize("platform", ["suse"])
def test_no_arm64_agent_is_published_for_the_platforms_we_target(platform):
    """Why --arch still defaults to x86_64 rather than the host's architecture.

    As of 11.0.7 the release site publishes official native arm64 builds for
    `ubuntu` (`ubuntu24.arm64.deb`), `debian` (`debian13.arm64.deb`) and
    `rhel` (`rhe9.aarch64.rpm`). Before 11.0.7, `rhel` arm64 came only as an
    Amazon Linux-named rpm and `ubuntu`/`debian` fell back to the raspbian
    armhf deb -- see `_ARM64_RASPBIAN_FALLBACK_PLATFORMS`, which is never
    used from 11.0.7 on.

    `suse` is the one platform left with no arm64 option whatsoever -- so on
    Apple Silicon defaulting to the host architecture would still fail
    resolution there. The tool emulates and says so instead.

    If this test starts failing, that assumption has changed and defaulting
    --arch to the host architecture becomes worth doing.
    """
    from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError

    version = resolve_version_spec("11.0")

    with pytest.raises(ResolveError):
        artifact_for(version, platform=platform, arch="arm64")
