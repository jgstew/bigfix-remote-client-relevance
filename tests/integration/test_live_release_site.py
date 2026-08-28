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
    [("windows", "x86_64"), ("macos", "arm64"), ("ubuntu", "x86_64"), ("rhel", "x86_64")],
)
def test_artifact_urls_are_live(platform, arch):
    import requests

    version = resolve_version_spec("11.0")
    artifact = artifact_for(version, platform=platform, arch=arch)

    assert artifact.sha256, "the release site should publish a checksum"

    response = requests.head(artifact.url, timeout=30, allow_redirects=True)
    assert response.status_code == 200, f"{artifact.url} returned {response.status_code}"


@pytest.mark.parametrize("platform", ["ubuntu", "debian", "rhel", "suse"])
def test_no_arm64_agent_is_published_for_the_platforms_we_target(platform):
    """Why --arch still defaults to x86_64 rather than the host's architecture.

    As of 11.0.6.137 the release site publishes amd64/x86_64, ppc64le, s390x
    and armhf (raspbian) builds, plus one aarch64 build under an `al2`
    (Amazon Linux) name that no TargetSpec maps to. For every platform this
    tool can actually select there is no arm64 agent — so on Apple Silicon
    defaulting to the host architecture would fail resolution on every run and
    fall back to x86_64 emulation anyway. The tool emulates and says so
    instead.

    If this test starts failing, that assumption has changed and defaulting
    --arch to the host architecture becomes worth doing.
    """
    from bigfix_remote_client_relevance.bootstrap.release_site import ResolveError

    version = resolve_version_spec("11.0")

    with pytest.raises(ResolveError):
        artifact_for(version, platform=platform, arch="arm64")
