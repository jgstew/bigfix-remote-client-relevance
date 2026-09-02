"""Tests for the online evaluator transport.

Exercises the JSON contract reverse-engineered from
https://developer.bigfix.com/relevance/evaluate/ against an injected fake
session -- no real network call, matching every other transport's fixture-
based unit tests (see DESIGN.md § Testing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports.online_evaluator import (
    TransportOnlineEvaluator,
)


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    raw_text: str | None = None

    @property
    def text(self) -> str:
        if self.raw_text is not None:
            return self.raw_text
        return json.dumps(self.body)

    def json(self) -> Any:
        if self.raw_text is not None:
            # Mirrors requests.Response.json() raising on unparsable text.
            raise ValueError("not valid json")
        return self.body


@dataclass
class FakeSession:
    """Stands in for requests.Session; queues one response/exception per call."""

    queue: list[FakeResponse | Exception] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object], float]] = field(default_factory=list)

    def post(self, url: str, json: dict[str, object], timeout: float) -> FakeResponse:
        self.calls.append((url, json, timeout))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok_response(*, answers: list[str], result_type: str, time_ms: int) -> FakeResponse:
    return FakeResponse(
        status_code=200,
        body={"answers": answers, "errors": [], "time": time_ms, "type": result_type},
    )


def relevance_error_response(message: str) -> FakeResponse:
    return FakeResponse(
        status_code=200,
        body={"answers": [], "errors": [message], "time": 0, "type": ""},
    )


async def test_success_maps_all_fields():
    session = FakeSession(
        queue=[
            ok_response(
                answers=["Linux Red Hat Enterprise Linux 8.1"], result_type="string", time_ms=255
            )
        ]
    )
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("name of operating system")

    assert result.host == "developer.bigfix.com"
    assert result.transport == "online_evaluator"
    assert result.answers == ["Linux Red Hat Enterprise Linux 8.1"]
    assert result.answer_types == ["string"]
    assert result.qna_time == "255"
    assert result.error is None
    assert result.error_kind is None
    assert result.exit_code == 0
    assert result.qna_path == "https://developer.bigfix.com/api/relevance/evaluate"
    assert len(session.calls) == 1
    url, body, _timeout = session.calls[0]
    assert url == "https://developer.bigfix.com/api/relevance/evaluate"
    assert body == {"relevance": "name of operating system"}


async def test_evaluate_does_not_set_arch_itself():
    """arch comes from resolve_arch, called externally by orchestrate.py --
    not something evaluate_client_relevance decides on its own."""
    session = FakeSession(queue=[ok_response(answers=["x"], result_type="string", time_ms=1)])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("true")

    assert result.arch is None


async def test_resolve_arch_probes_via_relevance():
    session = FakeSession(queue=[ok_response(answers=["x86_64"], result_type="string", time_ms=1)])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    arch = await transport.resolve_arch(timeout_s=5.0)

    assert arch == "x86_64"
    _url, body, timeout = session.calls[0]
    assert body == {"relevance": "architecture of the operating system"}
    assert timeout == 5.0


async def test_resolve_arch_raises_on_a_relevance_error():
    session = FakeSession(queue=[relevance_error_response("bad")])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    with pytest.raises(RuntimeError):
        await transport.resolve_arch()


async def test_resolve_arch_raises_when_unreachable():
    session = FakeSession(queue=[requests.ConnectionError("nope")])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    with pytest.raises(RuntimeError):
        await transport.resolve_arch()


async def test_explicit_host_overrides_url_hostname():
    session = FakeSession(queue=[ok_response(answers=["x"], result_type="string", time_ms=1)])
    transport = TransportOnlineEvaluator(
        "https://developer.bigfix.com", host="web-eval", session=session
    )

    result = await transport.evaluate_client_relevance("true")

    assert result.host == "web-eval"


async def test_relevance_error_is_classified():
    session = FakeSession(
        queue=[
            relevance_error_response("This expression contained a character which is not allowed.")
        ]
    )
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("this is not valid relevance ]]]")

    assert result.error_kind == ERROR_KIND_RELEVANCE
    assert result.error == "This expression contained a character which is not allowed."
    assert result.answers == []


async def test_empty_plural_result_is_not_an_error():
    """A legitimate zero-answer result (time > 0) must not be misclassified."""
    session = FakeSession(queue=[ok_response(answers=[], result_type="", time_ms=137)])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("names of bulk-serving objects")

    assert result.error_kind is None
    assert result.answers == []
    assert result.answer_types == []


async def test_502_is_retried_then_succeeds():
    session = FakeSession(
        queue=[
            FakeResponse(status_code=502, raw_text="Bad Gateway"),
            ok_response(answers=["x"], result_type="string", time_ms=10),
        ]
    )
    transport = TransportOnlineEvaluator(
        "https://developer.bigfix.com", session=session, max_retries=1
    )

    result = await transport.evaluate_client_relevance("true", timeout_s=1.0)

    assert result.error_kind is None
    assert result.answers == ["x"]
    assert len(session.calls) == 2


async def test_502_exhausts_retries_and_reports_transport_error():
    session = FakeSession(
        queue=[
            FakeResponse(status_code=502, raw_text="Bad Gateway"),
            FakeResponse(status_code=502, raw_text="Bad Gateway"),
        ]
    )
    transport = TransportOnlineEvaluator(
        "https://developer.bigfix.com", session=session, max_retries=1
    )

    result = await transport.evaluate_client_relevance("true", timeout_s=1.0)

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert "502" in (result.error or "")
    assert len(session.calls) == 2


async def test_non_502_status_is_not_retried():
    session = FakeSession(queue=[FakeResponse(status_code=404, raw_text="not found")])
    transport = TransportOnlineEvaluator(
        "https://developer.bigfix.com", session=session, max_retries=3
    )

    result = await transport.evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert "404" in (result.error or "")
    assert len(session.calls) == 1


async def test_connection_error_is_reported_without_raising():
    session = FakeSession(queue=[requests.ConnectionError("connection refused")])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert "connection refused" in (result.error or "")


async def test_malformed_json_body_is_a_transport_error():
    session = FakeSession(queue=[FakeResponse(status_code=200, raw_text="not json")])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_version_pinning_is_rejected():
    session = FakeSession(queue=[])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance(
        "true", qna=ResolvedQna(version="11.0.6.137", artifact_path=Path("/cache/x.zip"))
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "version" in (result.error or "").lower()
    assert session.calls == []  # refused before making any HTTP call


async def test_qna_path_override_is_ignored_not_fatal():
    session = FakeSession(queue=[ok_response(answers=["x"], result_type="string", time_ms=1)])
    transport = TransportOnlineEvaluator("https://developer.bigfix.com", session=session)

    result = await transport.evaluate_client_relevance("true", qna_path="/some/local/qna")

    assert result.error_kind is None


async def test_base_url_is_required():
    try:
        TransportOnlineEvaluator("")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty base_url")
