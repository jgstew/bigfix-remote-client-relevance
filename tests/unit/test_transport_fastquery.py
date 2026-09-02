"""Tests for the Fast Query transport stub.

Only the constructor signature and the version-pinning refusal are nailed down,
so that implementing Fast Query later does not reshape callers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bigfix_remote_client_relevance.results import ERROR_KIND_BOOTSTRAP, ResolvedQna
from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery


class FakeBesapiClient:
    """Stands in for a besapi session; the stub never calls it."""


async def test_constructor_signature_is_fixed():
    transport = TransportFastQuery(FakeBesapiClient(), computer_query='name of it = "lab-1"')

    assert transport is not None


async def test_evaluate_reports_not_implemented():
    transport = TransportFastQuery(FakeBesapiClient())

    result = await transport.evaluate_client_relevance("name of operating system")

    assert result.transport == "fastquery"
    assert result.error_kind is not None
    assert "not implemented" in (result.error or "").lower()


async def test_version_pinning_is_rejected():
    """Fast Query uses whatever agent is installed; a version cannot be chosen."""
    transport = TransportFastQuery(FakeBesapiClient())

    result = await transport.evaluate_client_relevance(
        "true", qna=ResolvedQna(version="11.0.6.137", artifact_path=Path("/cache/x.zip"))
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "version" in (result.error or "").lower()


async def test_no_exception_escapes():
    """Like every transport, failures come back as results."""
    transport = TransportFastQuery(FakeBesapiClient())

    result = await transport.evaluate_client_relevance("true")

    assert result.error_kind is not None
    assert result.client_relevance == "true"


async def test_resolve_arch_raises_since_evaluate_is_unimplemented():
    """Wired ahead of the transport itself: probe_arch_via_relevance just calls
    evaluate_client_relevance, which always errors today, so this always
    raises -- orchestrate.py's generic fallback ("x86_64") applies, exactly
    as if resolve_arch did not exist. No change needed here once Fast Query
    is actually implemented."""
    transport = TransportFastQuery(FakeBesapiClient())

    with pytest.raises(RuntimeError):
        await transport.resolve_arch()
