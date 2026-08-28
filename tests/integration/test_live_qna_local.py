"""Live checks against a real qna binary on this machine.

Auto-skipped unless a qna binary is present (and, on macOS, unless running as
root - qna needs it for some inspectors):

    sudo -E uv run pytest -m live_qna
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.results import ERROR_KIND_RELEVANCE
from bigfix_remote_client_relevance.transports.local import TransportLocal

pytestmark = pytest.mark.live_qna


async def test_true_evaluates_to_true():
    result = await TransportLocal().evaluate_client_relevance("TRUE")

    assert result.error_kind is None
    assert result.answers == ["True"]


async def test_operating_system_answers_something():
    result = await TransportLocal().evaluate_client_relevance("name of operating system")

    assert result.error_kind is None
    assert result.answers and result.answers[0]


async def test_showtypes_populates_answer_types():
    result = await TransportLocal().evaluate_client_relevance("version of client")

    assert result.error_kind is None
    assert result.answer_types, "real qna with -showtypes should emit an I: line"


async def test_timing_flag_populates_qna_time():
    result = await TransportLocal().evaluate_client_relevance("TRUE")

    assert result.qna_time, "real qna with -t should emit a T: line"


async def test_bad_inspector_reports_relevance_error():
    result = await TransportLocal().evaluate_client_relevance("namez of operating system")

    assert result.error_kind == ERROR_KIND_RELEVANCE
    assert result.error
