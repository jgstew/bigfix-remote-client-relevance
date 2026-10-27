"""BigFix Client Fast Query transport, through REST ClientQuery or BigFix SaaS.

Fast Query has the server push a client-relevance query out to managed
endpoints through relays; each endpoint evaluates it with its own installed
BES agent and the answers accumulate on the server. The wire layer -- config,
credentials, certificate pinning, and the submit/results calls (REST
``/api/clientquery``, SaaS ``clientquery/create``) -- belongs to
bigfix-remote-session-relevance
(:class:`~bigfix_remote_session_relevance.ClientQueryExtras`). This module owns
what that package deliberately leaves to the caller: which computers to target,
polling until they answer or time runs out, and turning rows into one
:class:`ClientRelevanceResult` per computer.

**BigFix SaaS takes one computer per query.** Through a ``SaasBackend`` the
transport sends one query per computer and polls each on its own, all
against the one deadline, with at most :data:`MAX_CONCURRENT_CALLS` server
calls in flight. A failed submit is that computer's result and is never
resent: SaaS answers most failures with a bare 500, possibly after the query
reached the root server.

**Version pinning does not apply to this transport.** Fast Query evaluates with
whatever BES agent happens to be installed on each endpoint; there is no way to
choose a qna version. That is inherent to the mechanism, not a first-cut
limitation, so passing a resolved version is an error rather than a hint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
)

if TYPE_CHECKING:
    from typing import Protocol

    from bigfix_remote_session_relevance import (
        ClientQueryExtras,
        ClientQueryRow,
        SessionRelevanceResult,
    )

    class FastQueryBackend(ClientQueryExtras, Protocol):
        """ClientQuery plus plain ``evaluate``, for ``computer_query``: a
        ``RestBackend`` or a ``SaasBackend``."""

        async def evaluate(self, session_relevance: str) -> SessionRelevanceResult: ...


logger = logging.getLogger(__name__)

TRANSPORT_NAME = "fastquery"

# Answers trickle in over seconds, so polling starts quick and backs off; the
# last check always lands on the timeout so a late answer still counts. Same
# schedule as the REST FastQuery in the BigFix relevance debugger console.
POLL_FIRST_S = 2.0
POLL_GROWTH = 1.5
POLL_MAX_S = 15.0

# Each server call is bounded by the time left, so a server that goes quiet
# cannot hold the run for session-relevance's own read timeout (120 s by
# default) per call. The floor still lets a call made at the very end -- the
# one results check after a slow submit -- have a moment to answer.
CALL_FLOOR_S = 5.0

# Server calls in flight at once while sending one query per computer, so a
# computer_query matching hundreds of endpoints does not open hundreds of
# submits together. Four concurrent submits drew no throttling and no
# rate-limit headers from BigFix SaaS (2026-10-27, DESIGN.md); larger bursts
# are untested, and a submit (~0.3-0.5 s) is short next to waiting for agents.
MAX_CONCURRENT_CALLS = 4

_T = TypeVar("_T")


class _CallTimeout(Exception):
    """A server call ran out of time; the message names which one."""


# What usually keeps an endpoint silent: the submit succeeds regardless,
# including for computers outside the operator's scope.
NO_ANSWER_CAUSES = "offline, UDP notifications blocked, or client/relay older than 9.5 Patch 2"


class TransportFastQuery:
    """Evaluate client relevance on managed endpoints via BigFix Fast Query.

    Unlike every other transport, one evaluation answers for many endpoints,
    so the native call is :meth:`evaluate_client_relevance_fanout` (the
    orchestrator's ``FanOutTransport``); :meth:`evaluate_client_relevance`
    stays for plain-``Transport`` callers and returns the first result.
    """

    def __init__(
        self,
        backend: FastQueryBackend,
        *,
        computer_ids: Sequence[int] = (),
        computer_query: str | None = None,
        use_client_context: bool | None = None,
        one_computer_per_query: bool | None = None,
        host: str = TRANSPORT_NAME,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        """
        Args:
            backend: A session-relevance backend with the ClientQuery calls
                (a ``RestBackend`` or ``SaasBackend``). Closed by :meth:`aclose`.
            computer_ids: BigFix computer IDs to target.
            computer_query: Session relevance selecting more computers; the
                server resolves it with ``ids of (...)`` under the same
                operator's scope. Note this one expression is *session*
                relevance -- the dialect that picks endpoints -- while
                everything evaluated on them is client relevance.
            use_client_context: ``True`` evaluates in the agent's client
                context (fixlet and site inspectors available) instead of
                the default qna-like one; ``None`` leaves ``UseClientContext``
                out of the request entirely, for servers that predate it
                (it is in the BESAPI 11.0.7.61 schema). Recorded on every
                result.
            one_computer_per_query: Send one query per computer instead of
                one for all. ``None`` decides from the backend's type: only
                a ``SaasBackend`` needs it. Decided before sending, never
                from the error: SaaS refuses several ids without sending
                anything, so that error says nothing about the computers.
            host: What a result is reported under when no computer can be
                (nothing targeted, or the submit failed).
            clock, sleep: Injected so tests never wait.
        """
        self._backend = backend
        self._computer_ids = list(computer_ids)
        self._computer_query = computer_query
        self._use_client_context = use_client_context
        if one_computer_per_query is None:
            from bigfix_remote_session_relevance import SaasBackend

            one_computer_per_query = isinstance(backend, SaasBackend)
        self._one_computer_per_query = one_computer_per_query
        self._host = host
        self._clock = clock
        self._sleep = sleep

    async def aclose(self) -> None:
        closer = getattr(self._backend, "aclose", None)
        if closer is not None:
            await closer()

    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,
        qna: ResolvedQna | None = None,
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult:
        """The first computer's result; see :meth:`evaluate_client_relevance_fanout`."""
        if qna is not None or qna_path is not None:
            asked = f"version {qna.version}" if qna is not None else f"qna at {qna_path}"
            return self._failure(
                client_relevance,
                ERROR_KIND_BOOTSTRAP,
                "Fast Query cannot pin a qna version: endpoints evaluate with "
                f"their installed BES agent, so {asked} cannot be requested. "
                "Use the ssh or container transport to pin a version.",
                started=self._clock(),
            )
        results = await self.evaluate_client_relevance_fanout(client_relevance, timeout_s=timeout_s)
        return results[0]

    async def evaluate_client_relevance_fanout(
        self, client_relevance: str, *, timeout_s: float = 30.0
    ) -> list[ClientRelevanceResult]:
        """One result per targeted computer; never raises.

        Answered computers come first, ordered by name; any that never
        answered follow in the order they were targeted.
        """
        started = self._clock()
        try:
            return await self._fanout(client_relevance, timeout_s=timeout_s, started=started)
        except Exception as exc:  # transports report, never raise
            logger.debug("fast query failed: %s", exc, exc_info=True)
            return [
                self._failure(
                    client_relevance, ERROR_KIND_TRANSPORT, f"{self._host}: {exc}", started=started
                )
            ]

    async def _fanout(
        self, client_relevance: str, *, timeout_s: float, started: float
    ) -> list[ClientRelevanceResult]:
        def fail(message: str) -> list[ClientRelevanceResult]:
            return [
                self._failure(
                    client_relevance,
                    ERROR_KIND_TRANSPORT,
                    f"{self._host}: {message}",
                    started=started,
                )
            ]

        deadline = started + timeout_s
        try:
            return await self._run(client_relevance, deadline, timeout_s, started, fail)
        except _CallTimeout as exc:
            return fail(str(exc))

    async def _call(self, what: str, call: Coroutine[Any, Any, _T], deadline: float) -> _T:
        """``call``, bounded by the time left before ``deadline`` (at least the floor).

        The floor never reaches past ``deadline + CALL_FLOOR_S``, one hard stop
        shared by every call: with one query per computer, every computer's
        last check falls due on the deadline and queues behind the cap, and a
        fresh floor for each would overrun by the floor times computers / cap.
        A call due after the hard stop is not made at all.
        """
        now = self._clock()
        limit = min(max(deadline - now, CALL_FLOOR_S), deadline + CALL_FLOOR_S - now)
        if limit <= 0:
            call.close()  # never started; closing it avoids "never awaited"
            raise _CallTimeout(f"{what} not checked: the timeout ran out first")
        try:
            return await asyncio.wait_for(call, timeout=limit)
        except TimeoutError:
            raise _CallTimeout(f"{what} timed out after {limit:g} s") from None

    async def _run(
        self,
        client_relevance: str,
        deadline: float,
        timeout_s: float,
        started: float,
        fail: Callable[[str], list[ClientRelevanceResult]],
    ) -> list[ClientRelevanceResult]:
        ids = list(self._computer_ids)
        if self._computer_query is not None:
            resolved = await self._call(
                "computer query",
                self._backend.evaluate(f"ids of ({self._computer_query})"),
                deadline,
            )
            if resolved.error is not None:
                return fail(f"computer query failed: {resolved.error.message}")
            ids.extend(int(answer) for answer in resolved.answer_strings)
        ids = list(dict.fromkeys(ids))  # merged, first appearance wins
        if not ids:
            return fail("no computers to query (the computer query matched none)")
        if self._one_computer_per_query:
            return await self._run_per_computer(client_relevance, ids, deadline, timeout_s, started)

        submitted = await self._call(
            "Fast Query submit",
            self._backend.submit_client_query(
                client_relevance, ids, use_client_context=self._use_client_context
            ),
            deadline,
        )
        if submitted.error is not None or submitted.query_id is None:
            message = submitted.error.message if submitted.error else "no query id returned"
            return fail(f"Fast Query submit failed: {message}")
        logger.info("Fast Query %s submitted to %d computer(s)", submitted.query_id, len(ids))

        rows, poll_error = await self._poll(submitted.query_id, set(ids), deadline)
        silence = poll_error or f"no answer within {timeout_s:g} s ({NO_ANSWER_CAUSES})"
        return self._merge(client_relevance, ids, rows, dict.fromkeys(ids, silence), started)

    async def _run_per_computer(
        self,
        client_relevance: str,
        ids: list[int],
        deadline: float,
        timeout_s: float,
        started: float,
    ) -> list[ClientRelevanceResult]:
        """One query per computer, side by side, merged like one query's rows."""
        logger.info(
            "Fast Query: one query per computer for %d computer(s) "
            "(BigFix SaaS takes one computer per query)",
            len(ids),
        )
        calls = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        outcomes = await asyncio.gather(
            *(
                self._one_computer(client_relevance, computer_id, deadline, timeout_s, calls)
                for computer_id in ids
            )
        )
        rows = [result_row for computer_rows, _ in outcomes for result_row in computer_rows]
        reasons = {
            computer_id: reason
            for computer_id, (_, reason) in zip(ids, outcomes, strict=True)
            if reason is not None
        }
        return self._merge(client_relevance, ids, rows, reasons, started)

    async def _one_computer(
        self,
        client_relevance: str,
        computer_id: int,
        deadline: float,
        timeout_s: float,
        calls: asyncio.Semaphore,
    ) -> tuple[list[ClientQueryRow], str | None]:
        """This computer's rows, or why it has none; never raises.

        Anything going wrong here -- a refused submit, a raise, a call that
        times out -- is this computer's alone; the others carry on.
        """
        try:
            async with calls:
                if self._clock() >= deadline:
                    # Sent this late, its answer could not count anyway.
                    return [], f"not submitted: the {timeout_s:g} s timeout ran out first"
                submitted = await self._call(
                    "Fast Query submit",
                    self._backend.submit_client_query(
                        client_relevance,
                        [computer_id],
                        use_client_context=self._use_client_context,
                    ),
                    deadline,
                )
        except _CallTimeout as exc:  # already says it was the submit
            return [], str(exc)
        except Exception as exc:  # this computer's failure, not the run's
            logger.debug("Fast Query submit to computer %d raised", computer_id, exc_info=True)
            return [], f"Fast Query submit failed: {exc}"
        if submitted.error is not None or submitted.query_id is None:
            # Never resent: SaaS answers a bare 500 for most failures,
            # possibly after the query reached the root server.
            message = submitted.error.message if submitted.error else "no query id returned"
            return [], f"Fast Query submit failed: {message}"
        logger.debug("Fast Query %s submitted to computer %d", submitted.query_id, computer_id)
        try:
            rows, poll_error = await self._poll(
                submitted.query_id, {computer_id}, deadline, calls=calls
            )
        except Exception as exc:  # this computer's failure, not the run's
            logger.debug("Fast Query results for computer %d raised", computer_id, exc_info=True)
            return [], f"Fast Query results failed: {exc}"
        if any(result_row.computer_id == computer_id for result_row in rows):
            return rows, None
        return rows, poll_error or f"no answer within {timeout_s:g} s ({NO_ANSWER_CAUSES})"

    def _merge(
        self,
        client_relevance: str,
        ids: list[int],
        rows: list[ClientQueryRow],
        reasons: dict[int, str],
        started: float,
    ) -> list[ClientRelevanceResult]:
        """Answered computers by name, then the rest in targeting order.

        ``reasons`` says why each unanswered computer has no answer.
        """
        by_computer: dict[int, list[ClientQueryRow]] = {}
        for result_row in rows:
            by_computer.setdefault(result_row.computer_id, []).append(result_row)
        elapsed_ms = int((self._clock() - started) * 1000)

        answered = sorted(
            (
                self._computer_result(client_relevance, group, elapsed_ms)
                for group in by_computer.values()
            ),
            key=lambda result: result.host.lower(),
        )
        unanswered = [
            ClientRelevanceResult(
                host=str(computer_id),
                transport=TRANSPORT_NAME,
                client_relevance=client_relevance,
                error=reasons[computer_id],
                error_kind=ERROR_KIND_TRANSPORT,
                elapsed_ms=elapsed_ms,
                use_client_context=self._use_client_context,
            )
            for computer_id in ids
            if computer_id not in by_computer
        ]
        return answered + unanswered

    async def _poll(
        self,
        query_id: int,
        targeted: set[int],
        deadline: float,
        *,
        calls: asyncio.Semaphore | None = None,
    ) -> tuple[list[ClientQueryRow], str | None]:
        """Every row so far, polled until all targets answered or time is up.

        Results accumulate server-side, so each poll returns everything and
        the latest one wins. There is always at least one check, even when
        resolving and submitting used up the whole timeout: computers that
        answered must not be reported as silent. ``calls``, when given, is
        held for each results call, never across a sleep. A poll error ends polling:
        the observed ones (a query id from another operator, say) do not
        clear up on retry. A poll that times out ends it too, keeping the
        rows already seen.
        """
        delay = POLL_FIRST_S
        rows: list[ClientQueryRow] = []
        checked = False
        while not checked or deadline - self._clock() > 0:
            left = deadline - self._clock()
            if left > 0:
                await self._sleep(min(delay, left))
                delay = min(delay * POLL_GROWTH, POLL_MAX_S)
            checked = True
            try:
                async with calls or contextlib.nullcontext():
                    response = await self._call(
                        "Fast Query results",
                        self._backend.client_query_results(query_id),
                        deadline,
                    )
            except _CallTimeout as exc:
                return rows, str(exc)
            if response.error is not None:
                return rows, f"Fast Query results failed: {response.error.message}"
            rows = response.rows
            if targeted <= {result_row.computer_id for result_row in rows}:
                break
        return rows, None

    def _computer_result(
        self, client_relevance: str, rows: list[ClientQueryRow], elapsed_ms: int
    ) -> ClientRelevanceResult:
        first = rows[0]
        failures = [r.result for r in rows if r.is_failure]
        return ClientRelevanceResult(
            host=first.computer_name or str(first.computer_id),
            transport=TRANSPORT_NAME,
            client_relevance=client_relevance,
            answers=[r.result for r in rows if not r.is_failure],
            # The REST results carry no types; [] rather than guessing.
            answer_types=[],
            error="; ".join(failures) if failures else None,
            error_kind=ERROR_KIND_RELEVANCE if failures else None,
            raw_qna_output=json.dumps([r.to_dict() for r in rows]),
            elapsed_ms=elapsed_ms,
            use_client_context=self._use_client_context,
        )

    def _failure(
        self, client_relevance: str, kind: str, message: str, *, started: float
    ) -> ClientRelevanceResult:
        return ClientRelevanceResult(
            host=self._host,
            transport=TRANSPORT_NAME,
            client_relevance=client_relevance,
            error=message,
            error_kind=kind,
            elapsed_ms=int((self._clock() - started) * 1000),
            use_client_context=self._use_client_context,
        )


__all__ = ["MAX_CONCURRENT_CALLS", "NO_ANSWER_CAUSES", "TransportFastQuery"]
