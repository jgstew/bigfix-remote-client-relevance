"""Every exception this package raises shares one catchable base.

The fan-out entry points never raise for target failures — they return results
with ``error_kind`` set. But the *setup* path does raise: loading an inventory,
resolving a version spec, priming the artifact cache. A consumer wrapping that
path wants one ``except`` clause, not eight imports.
"""

from __future__ import annotations

import importlib

import pytest

from bigfix_remote_client_relevance.exceptions import BigFixRelevanceError

# (module, class name) for every exception the package defines. Each must keep
# its own identity and its original import location -- the base class is
# additive, not a replacement.
PACKAGE_EXCEPTIONS = [
    ("bigfix_remote_client_relevance.inventory", "InventoryError"),
    ("bigfix_remote_client_relevance.bootstrap.release_site", "ResolveError"),
    ("bigfix_remote_client_relevance.bootstrap.cache", "ArtifactCacheError"),
    ("bigfix_remote_client_relevance.bootstrap.targets", "UnknownTargetError"),
    ("bigfix_remote_client_relevance.bootstrap.provision", "BootstrapFailure"),
    ("bigfix_remote_client_relevance.bootstrap.extract_local", "LocalExtractionError"),
    ("bigfix_remote_client_relevance.transports.ssh", "SSHConnectionError"),
    ("bigfix_remote_client_relevance.transports.container", "ContainerEngineError"),
]


@pytest.mark.parametrize(("module", "name"), PACKAGE_EXCEPTIONS)
def test_exception_derives_from_the_shared_base(module, name):
    exc = getattr(importlib.import_module(module), name)

    assert issubclass(exc, BigFixRelevanceError)
    assert issubclass(exc, Exception)


@pytest.mark.parametrize(("module", "name"), PACKAGE_EXCEPTIONS)
def test_exception_is_still_catchable_by_its_own_type(module, name):
    exc = getattr(importlib.import_module(module), name)

    with pytest.raises(exc):
        raise exc("boom")


def test_base_is_not_raised_directly_in_place_of_a_specific_kind():
    """The base is a category, not a stand-in: it stays distinct from its members."""
    from bigfix_remote_client_relevance.inventory import InventoryError

    assert InventoryError is not BigFixRelevanceError
    with pytest.raises(BigFixRelevanceError):
        raise InventoryError("boom")
