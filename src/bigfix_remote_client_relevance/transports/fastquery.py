"""BigFix Client Fast Query transport — stub.

Fast Query has the server push a client-relevance query out to endpoints
through relays. It is a legitimate third transport, but unlike SSH and
containers it needs a live BigFix deployment and gates on operator
permissions, so it is deliberately unimplemented here.

What this module *does* provide is a fixed constructor signature and the
version-pinning refusal, so landing Fast Query later does not reshape callers.

**Version pinning does not apply to this transport.** Fast Query evaluates with
whatever BES agent happens to be installed on each endpoint; there is no way to
choose a qna version. That is inherent to the mechanism, not a first-cut
limitation, so passing a resolved version is an error rather than a hint.
"""

from __future__ import annotations

import logging
import time

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ClientRelevanceResult,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports import probe_arch_via_relevance

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "fastquery"


class TransportFastQuery:
    """Placeholder for evaluating client relevance via BigFix Fast Query."""

    def __init__(
        self,
        besapi_client: object,
        *,
        computer_query: str | None = None,
    ) -> None:
        """
        Args:
            besapi_client: An authenticated besapi session.
            computer_query: Session relevance selecting which computers to
                target. Note this one expression is *session* relevance — the
                dialect that picks endpoints — while everything evaluated on
                them is client relevance.
        """
        self._besapi_client = besapi_client
        self._computer_query = computer_query

    async def resolve_arch(self, *, timeout_s: float = 30.0) -> str:
        """Probe a target's architecture via relevance, once implemented.

        Wired now, ahead of the transport itself, so it needs no change when
        Fast Query lands: :func:`~..transports.probe_arch_via_relevance` just
        calls :meth:`evaluate_client_relevance`, which today always errors
        (see class docstring) -- so this always raises for now, and
        orchestrate.py's generic arch-probe machinery falls back to its usual
        ``"x86_64"`` default, exactly as if this method did not exist.
        """
        return await probe_arch_via_relevance(self.evaluate_client_relevance, timeout_s=timeout_s)

    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,
        qna: ResolvedQna | None = None,
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult:
        started = time.monotonic()

        # API boundary: the BigFix REST payload calls this key `Relevance`;
        # the internal name stays `client_relevance`.
        if qna is not None:
            error = (
                "Fast Query cannot pin a qna version: endpoints evaluate with "
                f"their installed BES agent, so version {qna.version} cannot be "
                "requested. Use the ssh or container transport to pin a version."
            )
        else:
            error = (
                "the Fast Query transport is not implemented yet; use the ssh, "
                "container, or local transport"
            )

        return ClientRelevanceResult(
            host="fastquery",
            transport=TRANSPORT_NAME,
            client_relevance=client_relevance,
            error=error,
            error_kind=ERROR_KIND_BOOTSTRAP,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = ["TransportFastQuery"]
