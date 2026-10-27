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
        self.contexts: list[bool | None] = []
        self.polled: list[int] = []
        self.evaluated: list[str] = []

    async def submit_client_query(self, client_relevance, computer_ids, *, use_client_context):
        # Keyword required, no default: a transport that stops passing it
        # (and so silently drops a requested client context) fails loudly.
        self.submitted.append((client_relevance, list(computer_ids)))
        self.contexts.append(use_client_context)
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
        async def submit_client_query(self, client_relevance, computer_ids, **kwargs):
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
        async def submit_client_query(self, client_relevance, computer_ids, **kwargs):
            clock.now += 5.0
            return await super().submit_client_query(client_relevance, computer_ids, **kwargs)

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


# --- client context (UseClientContext) ---------------------------------------


async def test_client_context_is_left_unset_by_default():
    """None leaves <UseClientContext> out of the request, exactly as before
    the option existed, so servers that predate it keep working."""
    backend = FakeClientQueryBackend(polls=_polls_answering([row(11, "a")]))

    [result] = await make(backend, computer_ids=[11]).evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert backend.contexts == [None]
    assert result.use_client_context is None


@pytest.mark.parametrize("use_client_context", [True, False])
async def test_client_context_is_forwarded_and_recorded_on_every_result(use_client_context):
    """The same client relevance can answer differently in the two contexts,
    so every result, answered or not, says which one was asked for."""
    backend = FakeClientQueryBackend(polls=_polls_answering([row(11, "a", name="alpha")]))

    results = await make(
        backend, computer_ids=[11, 99], use_client_context=use_client_context
    ).evaluate_client_relevance_fanout("true", timeout_s=20)

    assert backend.contexts == [use_client_context]
    assert {r.host for r in results} == {"alpha", "99"}
    assert all(r.use_client_context is use_client_context for r in results)


async def test_client_context_is_recorded_on_a_failure_before_submitting():
    backend = FakeClientQueryBackend(ids=[])

    [result] = await make(
        backend, computer_query="bes computers whose (false)", use_client_context=True
    ).evaluate_client_relevance_fanout("true", timeout_s=30)

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert result.use_client_context is True


# --- one computer per query (BigFix SaaS) -----------------------------------


class VirtualClock:
    """Fake time shared by concurrent sleepers.

    :class:`FakeClock` adds every sleep to one total, which is right for one
    task but would make five computers polling side by side look five times
    as slow. Here time jumps to the earliest wake-up only once every other
    task has had its turn, so concurrent sleeps overlap the way real ones do.
    """

    def __init__(self) -> None:
        import itertools

        self.now = 0.0
        self.sleeps: list[float] = []
        self._waiting: list[tuple[float, int, object]] = []
        self._order = itertools.count()
        self._ticker = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        import asyncio
        import heapq

        self.sleeps.append(seconds)
        woken = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiting, (self.now + seconds, next(self._order), woken))
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.ensure_future(self._tick())
        await woken

    async def _tick(self) -> None:
        import asyncio
        import heapq

        while self._waiting:
            for _ in range(50):  # every runnable task reaches its next sleep
                await asyncio.sleep(0)
            wake, _, woken = heapq.heappop(self._waiting)
            self.now = max(self.now, wake)
            woken.set_result(None)  # type: ignore[attr-defined]


class FakeOneTargetBackend(FakeClientQueryBackend):
    """Takes one computer per query, the way ``SaasBackend`` does.

    Several ids are an ``unsupported_feature`` / ``multiple_targets`` result
    and ids <= 0 raise ``ValueError``, both before anything is "sent". Each
    submit gets its own query id, ``100 + computer id``; ``answers`` maps a
    computer id to the rows its query returns (after ``answer_after`` polls),
    and ``refuse`` maps one to the error its submit returns.
    """

    def __init__(
        self,
        *,
        answers: dict[int, Sequence[ClientQueryRow]] | None = None,
        refuse: dict[int, ResultError] | None = None,
        answer_after: int = 1,
        ids: Sequence[str] | ResultError = (),
    ) -> None:
        super().__init__(ids=ids)
        self._answers = answers or {}
        self._refuse = refuse or {}
        self._answer_after = answer_after
        self._polls_by_query: dict[int, int] = {}
        self.in_flight = 0
        self.most_in_flight = 0

    async def _busy(self) -> None:
        import asyncio

        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        for _ in range(3):  # stay in flight while the others get going
            await asyncio.sleep(0)
        self.in_flight -= 1

    async def submit_client_query(self, client_relevance, computer_ids, *, use_client_context):
        ids = list(computer_ids)
        if any(computer_id <= 0 for computer_id in ids):
            raise ValueError(f"computer_ids must be positive, got {ids}")
        if len(ids) > 1:
            return _response(
                error=ResultError(
                    kind="unsupported_feature",
                    code="multiple_targets",
                    message="BigFix SaaS fast query targets one computer per query",
                )
            )
        await self._busy()
        self.submitted.append((client_relevance, ids))
        self.contexts.append(use_client_context)
        if ids[0] in self._refuse:
            return _response(error=self._refuse[ids[0]])
        return _response(query_id=100 + ids[0])

    async def client_query_results(self, query_id):
        await self._busy()
        self.polled.append(query_id)
        polls = self._polls_by_query[query_id] = self._polls_by_query.get(query_id, 0) + 1
        computer_id = query_id - 100
        if polls < self._answer_after or computer_id not in self._answers:
            return _response(query_id=query_id)
        return _response(query_id=query_id, rows=self._answers[computer_id])


SERVER_500 = ResultError(
    kind="http",
    http_status=500,
    message=(
        "Server Error (BigFix SaaS answers 500 for any failed client query submit, "
        "including client relevance the root server rejects)"
    ),
)


def make_one_target(backend, clock=None, **kwargs) -> TransportFastQuery:
    clock = clock or VirtualClock()
    return TransportFastQuery(
        backend, clock=clock, sleep=clock.sleep, one_computer_per_query=True, **kwargs
    )


async def test_one_target_backend_gets_one_query_per_computer():
    backend = FakeOneTargetBackend(
        answers={11: [row(11, "a")], 12: [row(12, "b")], 13: [row(13, "c")]}
    )

    results = await make_one_target(
        backend, computer_ids=[11, 12, 13]
    ).evaluate_client_relevance_fanout("name of operating system", timeout_s=30)

    assert sorted(backend.submitted) == [
        ("name of operating system", [11]),
        ("name of operating system", [12]),
        ("name of operating system", [13]),
    ]
    assert set(backend.polled) == {111, 112, 113}
    assert [r.answers for r in results] == [["a"], ["b"], ["c"]]
    assert all(r.error_kind is None for r in results)


async def test_one_target_computer_query_ids_each_get_their_own_query():
    backend = FakeOneTargetBackend(ids=["12", "13"], answers={12: [row(12, "b")]})

    await make_one_target(
        backend, computer_ids=[12], computer_query="bes computers"
    ).evaluate_client_relevance_fanout("true", timeout_s=30)

    assert backend.evaluated == ["ids of (bes computers)"]
    assert sorted(ids for _, ids in backend.submitted) == [[12], [13]]


async def test_one_target_results_answered_by_name_then_unanswered_in_targeting_order():
    backend = FakeOneTargetBackend(
        answers={
            30: [row(30, "m", name="mike")],
            11: [row(11, "z", name="zulu")],
            12: [row(12, "a", name="Alpha")],
        }
    )

    results = await make_one_target(
        backend, computer_ids=[30, 11, 99, 12, 98]
    ).evaluate_client_relevance_fanout("true", timeout_s=20)

    assert [r.host for r in results] == ["Alpha", "mike", "zulu", "99", "98"]
    assert results[3].error == (
        "no answer within 20 s (offline, UDP notifications blocked, or "
        "client/relay older than 9.5 Patch 2)"
    )


async def test_one_target_submit_failure_is_that_computers_result_and_never_resent():
    """SaaS answers a bare 500 for most failed submits, possibly after the
    query reached the root server: a resend could run it twice."""
    backend = FakeOneTargetBackend(
        answers={11: [row(11, "a", name="alpha")], 13: [row(13, "c", name="charlie")]},
        refuse={12: SERVER_500},
    )

    results = await make_one_target(
        backend, computer_ids=[11, 12, 13]
    ).evaluate_client_relevance_fanout("true", timeout_s=30)

    assert [r.host for r in results] == ["alpha", "charlie", "12"]
    failed = results[2]
    assert failed.error_kind == ERROR_KIND_TRANSPORT
    assert failed.transport == "fastquery"
    assert failed.error is not None
    assert failed.error.startswith("Fast Query submit failed: Server Error")
    assert [ids for _, ids in backend.submitted].count([12]) == 1
    assert 112 not in backend.polled


async def test_one_target_exception_for_one_computer_spares_the_others():
    """A raise (here SaasBackend's ValueError for an id <= 0) fails only the
    computer it was raised for."""
    backend = FakeOneTargetBackend(answers={11: [row(11, "a", name="alpha")]})

    results = await make_one_target(backend, computer_ids=[11, 0]).evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert [r.host for r in results] == ["alpha", "0"]
    assert results[0].answers == ["a"]
    assert results[1].error_kind == ERROR_KIND_TRANSPORT
    assert (results[1].error or "").startswith("Fast Query submit failed: ")
    assert "must be positive" in (results[1].error or "")


async def test_one_target_queries_share_one_deadline():
    """Every per-computer query polls until the same --timeout, not a
    timeout each one after another."""
    backend = FakeOneTargetBackend()  # nobody ever answers
    clock = VirtualClock()

    results = await make_one_target(
        backend, clock, computer_ids=[11, 12, 13, 14, 15]
    ).evaluate_client_relevance_fanout("true", timeout_s=60)

    assert clock.now == pytest.approx(60.0)
    assert [r.host for r in results] == ["11", "12", "13", "14", "15"]
    assert all("no answer within 60 s" in (r.error or "") for r in results)
    # Each computer got the usual backoff, the last check on the timeout.
    assert sorted(backend.polled).count(111) == 8


async def test_one_target_never_submits_once_the_deadline_has_passed(monkeypatch):
    """Submits waiting their turn behind the cap are dropped at the deadline
    rather than sent late, when no answer could still count."""
    from bigfix_remote_client_relevance.transports import fastquery

    monkeypatch.setattr(fastquery, "MAX_CONCURRENT_CALLS", 1)
    clock = VirtualClock()

    class SlowSubmit(FakeOneTargetBackend):
        async def submit_client_query(self, client_relevance, computer_ids, **kwargs):
            clock.now += 8.0  # 12 finishes at 16: late, but within deadline + floor
            return await super().submit_client_query(client_relevance, computer_ids, **kwargs)

    backend = SlowSubmit(
        answers={11: [row(11, "a", name="alpha")], 12: [row(12, "b", name="bravo")]}
    )

    results = await make_one_target(
        backend, clock, computer_ids=[11, 12, 13]
    ).evaluate_client_relevance_fanout("true", timeout_s=15)

    assert [ids for _, ids in backend.submitted] == [[11], [12]]
    assert [r.host for r in results] == ["alpha", "bravo", "13"]
    assert results[2].error_kind == ERROR_KIND_TRANSPORT
    assert results[2].error == "not submitted: the 15 s timeout ran out first"


async def test_one_target_concurrency_is_capped():
    from bigfix_remote_client_relevance.transports.fastquery import MAX_CONCURRENT_CALLS

    ids = list(range(1, 3 * MAX_CONCURRENT_CALLS + 1))
    backend = FakeOneTargetBackend(answers={i: [row(i, "x")] for i in ids}, answer_after=2)

    results = await make_one_target(backend, computer_ids=ids).evaluate_client_relevance_fanout(
        "true", timeout_s=30
    )

    assert len(backend.submitted) == len(ids)
    assert backend.most_in_flight == MAX_CONCURRENT_CALLS
    assert all(r.answers == ["x"] for r in results)


@pytest.mark.parametrize("use_client_context", [True, False, None])
async def test_one_target_client_context_goes_on_every_submit(use_client_context):
    backend = FakeOneTargetBackend(answers={11: [row(11, "a", name="alpha")]})

    results = await make_one_target(
        backend, computer_ids=[11, 12, 13], use_client_context=use_client_context
    ).evaluate_client_relevance_fanout("true", timeout_s=20)

    assert backend.contexts == [use_client_context] * 3
    assert len(results) == 3
    assert all(r.use_client_context is use_client_context for r in results)


async def test_one_target_single_result_method_still_works():
    backend = FakeOneTargetBackend(answers={1: [row(1, "y", name="alpha")]})

    result = await make_one_target(backend, computer_ids=[1, 2]).evaluate_client_relevance(
        "true", timeout_s=30
    )

    assert result.host == "alpha"


# --- a real SaasBackend, offline ---------------------------------------------

SAAS_FIXTURES = Path(__file__).parent.parent / "fixtures" / "saas_fastquery"


class FakeSaasService:
    """Just enough of BigFix SaaS for ``SaasBackend``: sign-in, one
    ``clientquery/create`` per computer and ``listresults``, answering with
    the captured bodies. Computer 11 answers; any other id gets the captured
    500, as an id the deployment does not have did live."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def transport(self):
        import httpx

        return httpx.MockTransport(self._handle)

    def _handle(self, request):
        import httpx

        def captured(name: str, status: int = 200):
            return httpx.Response(
                status,
                text=(SAAS_FIXTURES / name).read_text(encoding="utf-8"),
                headers={"content-type": "application/json"},
            )

        data = json.loads(request.content or b"{}").get("data")
        if request.url.path == "/auth/v1/user/login":
            return httpx.Response(
                200,
                json={"status": 200, "data": {"iat": 0, "exp": 3600, "pids": {"bf": {}}}},
                headers=[
                    ("set-cookie", "accessToken=ACCESS; Path=/; HttpOnly; Secure"),
                    ("set-cookie", "refreshToken=REFRESH; Path=/; HttpOnly; Secure"),
                ],
            )
        if request.url.path == "/core/v2/clientquery/create":
            self.created.append(data)
            if data["target"] == {"computerId": 11}:
                return captured("create_ok.json")
            return captured("create_500.json", status=500)
        if request.url.path == "/bfproxy/v1/clientquery/listresults":
            return captured("listresults_answered.json")
        return httpx.Response(404)


async def test_a_saas_backend_is_fanned_out_without_being_told():
    """The backend's type decides it, before anything is sent: SaasBackend
    refuses several ids without sending them, so its error is no signal to
    retry on and must never be relied on."""
    from bigfix_remote_session_relevance import SaasBackend

    service = FakeSaasService()
    backend = SaasBackend(
        "login.bigfix.com",
        username="operator@example.com",
        password="<PASSWORD>",
        transport=service.transport(),
    )
    clock = VirtualClock()

    results = await TransportFastQuery(
        backend, computer_ids=[11, 12], clock=clock, sleep=clock.sleep
    ).evaluate_client_relevance_fanout("name of operating system", timeout_s=30)

    assert [entry["target"] for entry in service.created] == [
        {"computerId": 11},
        {"computerId": 12},
    ]
    assert [r.host for r in results] == ["pc11", "12"]
    assert results[0].answers == ["Win11"]
    assert results[1].error_kind == ERROR_KIND_TRANSPORT
    assert "500" in (results[1].error or "")


@pytest.mark.parametrize("hung", [True, False], ids=["hung-server", "slow-server"])
async def test_one_target_calls_after_the_deadline_share_one_hard_stop(monkeypatch, hung):
    """Every computer's last check lands on the deadline and they queue
    behind the cap; each getting its own CALL_FLOOR_S let a run overrun by
    the floor times computers / cap. All of them share deadline + floor."""
    import asyncio
    import time

    from bigfix_remote_client_relevance.transports import fastquery

    monkeypatch.setattr(fastquery, "CALL_FLOOR_S", 0.2)
    timeout_s = 0.2

    class Slow(FakeOneTargetBackend):
        async def client_query_results(self, query_id):
            self.polled.append(query_id)
            await asyncio.sleep(3600 if hung else 0.15)
            return _response(query_id=query_id)

    backend = Slow()
    ids = list(range(1, 4 * fastquery.MAX_CONCURRENT_CALLS + 1))
    transport = _real_time(backend, computer_ids=ids, one_computer_per_query=True)

    started = time.monotonic()
    results = await transport.evaluate_client_relevance_fanout("true", timeout_s=timeout_s)
    elapsed = time.monotonic() - started

    # deadline + floor = 0.4 s; a floor per queued call would be ~1 s.
    assert elapsed < 0.65
    assert [r.host for r in results] == [str(i) for i in ids]
    assert all(r.error_kind == ERROR_KIND_TRANSPORT for r in results)
    skipped = [r for r in results if "not checked" in (r.error or "")]
    assert skipped, [r.error for r in results]
    assert skipped[0].error == "Fast Query results not checked: the timeout ran out first"
