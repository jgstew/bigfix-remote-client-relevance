"""Tests for the Fast Query transport (BigFix REST ClientQuery).

The backend is faked down to the three calls the transport makes --
``evaluate`` (session relevance, for ``computer_query``),
``submit_client_query`` and ``client_query_results`` -- with scripted
responses. The clock and sleep are injected, so no test ever waits.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from bigfix_remote_session_relevance import (
    ClientQueryResponse,
    ClientQueryRow,
    ResultError,
    SessionRelevanceResult,
)

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery


def _response(
    *, query_id: int | None = None, rows: Sequence[ClientQueryRow] = (), error=None
) -> ClientQueryResponse:
    return ClientQueryResponse(
        query_id=query_id,
        rows=list(rows),
        error=error,
        connection="lab",
        server="bigfix.example:52311",
        backend="rest",
        elapsed_ms=1.0,
    )


def row(computer_id: int, result: str, *, name: str | None = None, failure: bool = False):
    return ClientQueryRow(computer_id, name or f"pc{computer_id}", failure, result)


class FakeClock:
    """A monotonic clock that only moves when the transport sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeClientQueryBackend:
    """Scripted ``submit_client_query`` / ``client_query_results`` / ``evaluate``.

    ``polls`` is the sequence of results responses; the last one repeats once
    the script runs out, the way a server keeps returning every row so far.
    """

    def __init__(
        self,
        *,
        polls: Sequence[ClientQueryResponse] = (),
        submit: ClientQueryResponse | None = None,
        ids: Sequence[str] | ResultError = (),
    ) -> None:
        self._polls = list(polls) or [_response(query_id=7)]
        self._submit = submit or _response(query_id=7)
        self._ids = ids
        self.submitted: list[tuple[str, list[int]]] = []
        self.polled: list[int] = []
        self.evaluated: list[str] = []

    async def submit_client_query(self, client_relevance, computer_ids):
        self.submitted.append((client_relevance, list(computer_ids)))
        return self._submit

    async def client_query_results(self, query_id):
        self.polled.append(query_id)
        index = min(len(self.polled), len(self._polls)) - 1
        return self._polls[index]

    async def evaluate(self, session_relevance):
        self.evaluated.append(session_relevance)
        if isinstance(self._ids, ResultError):
            return SessionRelevanceResult.failure(session_relevance, self._ids)
        return SessionRelevanceResult(
            session_relevance=session_relevance,
            answers=tuple(int(i) for i in self._ids),
            answer_strings=tuple(self._ids),
        )


def make(backend, clock=None, **kwargs) -> TransportFastQuery:
    clock = clock or FakeClock()
    return TransportFastQuery(backend, clock=clock, sleep=clock.sleep, **kwargs)


def _polls_answering(*rows_so_far: Sequence[ClientQueryRow]):
    return [_response(query_id=7, rows=rows) for rows in rows_so_far]


async def test_version_pinning_is_still_rejected():
    """Fast Query uses whatever agent is installed; a version cannot be chosen."""
    transport = make(FakeClientQueryBackend(), computer_ids=[1])

    result = await transport.evaluate_client_relevance(
        "true", qna=ResolvedQna(version="11.0.6.137", artifact_path=Path("/cache/x.zip"))
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "version" in (result.error or "").lower()


async def test_an_explicit_qna_path_is_rejected():
    transport = make(FakeClientQueryBackend(), computer_ids=[1])

    result = await transport.evaluate_client_relevance("true", qna_path="/opt/qna")

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "qna" in (result.error or "").lower()


async def test_submits_client_relevance_to_computer_ids():
    backend = FakeClientQueryBackend(polls=_polls_answering([row(11, "a"), row(12, "b")]))

    await make(backend, computer_ids=[11, 12]).evaluate_client_relevance_fanout(
        "name of operating system", timeout_s=30
    )

    assert backend.submitted == [("name of operating system", [11, 12])]
    assert backend.polled and set(backend.polled) == {7}


async def test_computer_query_resolves_ids_via_session_relevance():
    backend = FakeClientQueryBackend(
        ids=["12", "13"],
        polls=_polls_answering([row(11, "a"), row(12, "b"), row(13, "c")]),
    )

    await make(
        backend, computer_ids=[11, 12], computer_query='bes computers whose (name of it = "x")'
    ).evaluate_client_relevance_fanout("true", timeout_s=30)

    assert backend.evaluated == ['ids of (bes computers whose (name of it = "x"))']
    # Merged with the explicit IDs, in order, without repeating 12.
    assert backend.submitted == [("true", [11, 12, 13])]


async def test_computer_query_failure_is_one_transport_result():
    backend = FakeClientQueryBackend(
        ids=ResultError(kind="session_relevance", message="Singular expression refers to")
    )

    results = await make(backend, computer_query="bogus").evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_TRANSPORT
    assert "Singular expression refers to" in (results[0].error or "")
    assert backend.submitted == []


async def test_no_ids_is_one_transport_result():
    backend = FakeClientQueryBackend(ids=[])

    results = await make(
        backend, computer_query="bes computers whose (false)", host="lab"
    ).evaluate_client_relevance_fanout("true", timeout_s=30)

    assert len(results) == 1
    assert results[0].host == "lab"
    assert results[0].transport == "fastquery"
    assert results[0].error_kind == ERROR_KIND_TRANSPORT
    assert "no computers" in (results[0].error or "")
    assert backend.submitted == []


async def test_submit_error_is_one_transport_result():
    backend = FakeClientQueryBackend(
        submit=_response(
            error=ResultError(
                kind="authentication",
                message='HTTP 403 (non-master operators need the "Can Submit Queries" permission)',
            )
        )
    )

    results = await make(backend, computer_ids=[11, 12]).evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_TRANSPORT
    assert "Can Submit Queries" in (results[0].error or "")
    assert backend.polled == []


async def test_rows_grouped_per_computer_plural_in_order():
    backend = FakeClientQueryBackend(
        polls=_polls_answering(
            [
                row(12, "eth0", name="beta"),
                row(11, "only", name="alpha"),
                row(12, "eth1", name="beta"),
            ]
        )
    )

    results = await make(backend, computer_ids=[11, 12]).evaluate_client_relevance_fanout(
        "names of network adapters", timeout_s=30
    )

    by_host = {r.host: r for r in results}
    assert set(by_host) == {"alpha", "beta"}
    assert by_host["beta"].answers == ["eth0", "eth1"]
    assert by_host["alpha"].answers == ["only"]
    for result in results:
        assert result.transport == "fastquery"
        assert result.answer_types == []
        assert result.error_kind is None
        assert result.client_relevance == "names of network adapters"
    assert json.loads(by_host["beta"].raw_qna_output) == [
        {"computer_id": 12, "computer_name": "beta", "is_failure": False, "result": "eth0"},
        {"computer_id": 12, "computer_name": "beta", "is_failure": False, "result": "eth1"},
    ]


async def test_results_are_ordered_by_host():
    backend = FakeClientQueryBackend(
        polls=_polls_answering([row(2, "x", name="zulu"), row(1, "y", name="alpha")])
    )

    results = await make(backend, computer_ids=[2, 1]).evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert [r.host for r in results] == ["alpha", "zulu"]


async def test_failure_row_is_relevance_error():
    backend = FakeClientQueryBackend(
        polls=_polls_answering(
            [row(11, "Singular expression refers to nonexistent object.", failure=True)]
        )
    )

    results = await make(backend, computer_ids=[11]).evaluate_client_relevance_fanout(
        'name of file "nope"', timeout_s=30
    )

    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_RELEVANCE
    assert results[0].error == "Singular expression refers to nonexistent object."
    assert results[0].answers == []


async def test_stops_when_every_id_answered():
    backend = FakeClientQueryBackend(
        polls=_polls_answering([], [row(11, "a")], [row(11, "a"), row(12, "b")])
    )
    clock = FakeClock()

    results = await make(backend, clock, computer_ids=[11, 12]).evaluate_client_relevance_fanout(
        "true", timeout_s=60
    )

    assert len(backend.polled) == 3
    assert clock.sleeps == [2.0, 3.0, 4.5]
    assert all(r.error_kind is None for r in results)


async def test_backoff_2s_x1_5_capped_15s_last_check_at_timeout():
    backend = FakeClientQueryBackend()  # nobody ever answers
    clock = FakeClock()

    await make(backend, clock, computer_ids=[11]).evaluate_client_relevance_fanout(
        "true", timeout_s=60
    )

    # 2, 3, 4.5, 6.75, 10.125, then capped at 15; the last wait is whatever
    # is left so the final check lands exactly on the timeout.
    assert clock.sleeps == pytest.approx([2.0, 3.0, 4.5, 6.75, 10.125, 15.0, 15.0, 3.625])
    assert clock.now == pytest.approx(60.0)
    assert len(backend.polled) == len(clock.sleeps)


async def test_unanswered_ids_are_transport_results_at_timeout():
    backend = FakeClientQueryBackend(polls=_polls_answering([row(11, "a", name="alpha")]))

    results = await make(backend, computer_ids=[11, 99]).evaluate_client_relevance_fanout(
        "true", timeout_s=20
    )

    by_host = {r.host: r for r in results}
    assert by_host["alpha"].answers == ["a"]
    missing = by_host["99"]
    assert missing.error_kind == ERROR_KIND_TRANSPORT
    assert missing.error == (
        "no answer within 20 s (offline, UDP notifications blocked, or "
        "client/relay older than 9.5 Patch 2)"
    )


async def test_poll_error_ends_polling_and_reports_unanswered_ids():
    backend = FakeClientQueryBackend(
        polls=[
            _response(query_id=7, rows=[row(11, "a", name="alpha")]),
            _response(error=ResultError(kind="http", message="HTTP 400: cannot get results")),
        ]
    )

    results = await make(backend, computer_ids=[11, 12]).evaluate_client_relevance_fanout(
        "true", timeout_s=60
    )

    assert len(backend.polled) == 2
    by_host = {r.host: r for r in results}
    assert by_host["alpha"].answers == ["a"]
    assert by_host["12"].error_kind == ERROR_KIND_TRANSPORT
    assert "cannot get results" in (by_host["12"].error or "")


async def test_no_exception_escapes():
    """Like every transport, failures come back as results."""

    class ExplodingBackend(FakeClientQueryBackend):
        async def submit_client_query(self, client_relevance, computer_ids):
            raise RuntimeError("socket closed")

    results = await make(ExplodingBackend(), computer_ids=[11]).evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_TRANSPORT
    assert "socket closed" in (results[0].error or "")
    assert results[0].client_relevance == "true"


async def test_single_result_method_still_works():
    """`evaluate_client_relevance` returns the first computer's result, so
    library callers holding a plain Transport keep working."""
    backend = FakeClientQueryBackend(
        polls=_polls_answering([row(2, "x", name="zulu"), row(1, "y", name="alpha")])
    )

    result = await make(backend, computer_ids=[1, 2]).evaluate_client_relevance(
        "true", timeout_s=30
    )

    assert result.host == "alpha"
    assert result.answers == ["y"]


async def test_aclose_closes_the_backend():
    class ClosingBackend(FakeClientQueryBackend):
        closed = False

        async def aclose(self):
            self.closed = True

    backend = ClosingBackend()
    await make(backend, computer_ids=[1]).aclose()

    assert backend.closed


async def test_results_are_checked_once_even_when_setup_used_the_whole_timeout():
    """A slow computer query or submit must not turn into "no answer" for
    computers that did answer: there is always one results call."""
    clock = FakeClock()

    class SlowSubmit(FakeClientQueryBackend):
        async def submit_client_query(self, client_relevance, computer_ids):
            clock.now += 5.0
            return await super().submit_client_query(client_relevance, computer_ids)

    backend = SlowSubmit(polls=_polls_answering([row(11, "a", name="alpha")]))

    results = await make(backend, clock, computer_ids=[11]).evaluate_client_relevance_fanout(
        "true", timeout_s=4
    )

    assert len(backend.polled) == 1
    assert clock.sleeps == []
    assert results[0].answers == ["a"]


class _Hang:
    """Never returns, like a server that accepted the connection and went quiet."""

    async def __call__(self, *args, **kwargs):
        import asyncio

        await asyncio.Event().wait()


def _real_time(backend, **kwargs) -> TransportFastQuery:
    return TransportFastQuery(backend, **kwargs)


@pytest.fixture
def short_call_floor(monkeypatch):
    from bigfix_remote_client_relevance.transports import fastquery

    monkeypatch.setattr(fastquery, "CALL_FLOOR_S", 0.05)


@pytest.mark.parametrize("call", ["evaluate", "submit_client_query", "client_query_results"])
async def test_a_hung_backend_call_is_bounded_by_the_timeout(short_call_floor, call):
    import time

    backend = FakeClientQueryBackend(ids=["11"])
    setattr(backend, call, _Hang())
    transport = _real_time(backend, computer_ids=[11], computer_query="bes computers")

    started = time.monotonic()
    results = await transport.evaluate_client_relevance_fanout("true", timeout_s=0.3)

    assert time.monotonic() - started < 2.0
    assert len(results) == 1
    assert results[0].error_kind == ERROR_KIND_TRANSPORT
    assert "timed out" in (results[0].error or "")


async def test_an_empty_plural_answer_is_one_empty_string():
    """The server sends one row with result "" for both an empty plural
    answer and ""; the two cannot be told apart, so neither becomes []."""
    backend = FakeClientQueryBackend(polls=_polls_answering([row(11, "", name="alpha")]))

    [result] = await make(backend, computer_ids=[11]).evaluate_client_relevance_fanout(
        'names of files whose (false) of folder "/tmp"', timeout_s=30
    )

    assert result.answers == [""]
    assert result.error_kind is None
