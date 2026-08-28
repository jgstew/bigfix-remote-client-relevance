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
