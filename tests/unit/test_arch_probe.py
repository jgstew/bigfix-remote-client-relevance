"""Tests for the relevance-based arch probe shared by transports with no
other side channel to ask (online_evaluator, fastquery) -- unlike local's
``sys.platform`` or ssh's ``uname -m``.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
)
from bigfix_remote_client_relevance.transports import (
    ARCH_PROBE_RELEVANCE,
    probe_arch_via_relevance,
)


def make_evaluate(result: ClientRelevanceResult):
    calls = []

    async def evaluate(client_relevance, *, timeout_s=30.0):
        calls.append((client_relevance, timeout_s))
        return result

    evaluate.calls = calls  # type: ignore[attr-defined]
    return evaluate


async def test_probes_using_the_expected_relevance_expression():
    evaluate = make_evaluate(
        ClientRelevanceResult(
            host="h", transport="x", client_relevance=ARCH_PROBE_RELEVANCE, answers=["x86_64"]
        )
    )

    arch = await probe_arch_via_relevance(evaluate, timeout_s=5.0)

    assert arch == "x86_64"
    assert evaluate.calls == [(ARCH_PROBE_RELEVANCE, 5.0)]


async def test_relevance_error_raises_instead_of_guessing():
    evaluate = make_evaluate(
        ClientRelevanceResult(
            host="h",
            transport="x",
            client_relevance=ARCH_PROBE_RELEVANCE,
            error="bad",
            error_kind=ERROR_KIND_RELEVANCE,
        )
    )

    with pytest.raises(RuntimeError):
        await probe_arch_via_relevance(evaluate)


async def test_transport_error_raises_instead_of_guessing():
    evaluate = make_evaluate(
        ClientRelevanceResult(
            host="h",
            transport="x",
            client_relevance=ARCH_PROBE_RELEVANCE,
            error="unreachable",
            error_kind=ERROR_KIND_TRANSPORT,
        )
    )

    with pytest.raises(RuntimeError):
        await probe_arch_via_relevance(evaluate)


async def test_empty_answer_raises_instead_of_guessing():
    evaluate = make_evaluate(
        ClientRelevanceResult(
            host="h", transport="x", client_relevance=ARCH_PROBE_RELEVANCE, answers=[]
        )
    )

    with pytest.raises(RuntimeError):
        await probe_arch_via_relevance(evaluate)
