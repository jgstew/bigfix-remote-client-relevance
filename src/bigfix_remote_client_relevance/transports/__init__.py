"""Transport interface.

Every transport answers the same question — "what does this client relevance
evaluate to over there?" — and returns the same result type, so callers do not
branch on transport kind.

Contract shared by all implementations:

* Failures never escape as exceptions. Each one becomes a
  :class:`~bigfix_remote_client_relevance.results.ClientRelevanceResult` with
  ``error_kind`` set, so a fan-out over many targets is never derailed by one
  bad host.
* Transports never resolve version specs or fetch artifacts. They receive a
  fully-resolved :class:`~bigfix_remote_client_relevance.results.ResolvedQna`,
  which keeps them offline-testable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bigfix_remote_client_relevance.results import ClientRelevanceResult, ResolvedQna


@runtime_checkable
class Transport(Protocol):
    """Evaluates client relevance somewhere — locally, over SSH, in a container."""

    async def evaluate_client_relevance(
        self,
        client_relevance: str,
        *,
        qna_path: str | None = None,
        qna: ResolvedQna | None = None,
        timeout_s: float = 30.0,
    ) -> ClientRelevanceResult:
        """Evaluate ``client_relevance`` and return the result.

        Args:
            client_relevance: The expression to evaluate.
            qna_path: Explicit binary to use; None discovers one on the target.
            qna: A resolved version to provision; None uses whatever qna the
                target already has.
            timeout_s: Caller-side bound on the evaluation.
        """
        ...


__all__ = ["Transport"]
