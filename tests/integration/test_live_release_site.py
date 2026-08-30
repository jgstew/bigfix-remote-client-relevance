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

    As of 11.0.6.137 the release site publishes amd64/x86_64, ppc64le, s390x
    and armhf (raspbian) builds. Two families have an arm64-capable
    workaround, both handled by `artifact_for`, neither a true native build:

    - `rhel` ships its arm64 client under an Amazon Linux-named filename
      (`al2.aarch64.rpm`, officially supported only there, but a plain rpm
      that runs on any rhel-family arm64 host) -- see the `rhel` entry in
      `_PLATFORM_PATTERNS`.
    - `ubuntu`/`debian` have no native arm64 build at all, but the raspbian
      armhf (32-bit ARM) deb runs under an arm64 kernel's 32-bit userspace
      compat -- generically on Debian, mostly on Ubuntu with some rough
      edges -- see `_ARM64_RASPBIAN_FALLBACK`.

    `suse` is the one platform left with no arm64 option whatsoever -- so on
    Apple Silicon defaulting to the host architecture would still fail
    resolution there, and fall back to x86_64 emulation anyway. The tool
    emulates and says so instead.

    If this test starts failing, that assumption has changed and defaulting
    --arch to the host architecture becomes worth doing.
    """
    from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError

    version = resolve_version_spec("11.0")

    with pytest.raises(ResolveError):
        artifact_for(version, platform=platform, arch="arm64")
