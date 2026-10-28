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

from collections.abc import Awaitable, Callable, Sequence
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


@runtime_checkable
class FanOutTransport(Protocol):
    """Optional: one evaluation answers for many endpoints.

    Every other transport is one target, one result. Fast Query is one
    target -- a BigFix server plus which computers to ask -- and one result
    per computer that answers, so the orchestrator emits each of these in
    place of the single ``evaluate_client_relevance`` result. Version pinning
    never applies, so there is no ``qna`` argument.

    So for a fan-out target the number of results is not known up front:
    :func:`~..orchestrate.count_work` counts one per cell, a floor rather
    than the exact total it is for every other transport.

    ``evaluate_many`` runs a fan-out target's expressions a few at once
    (:data:`~..orchestrate.FANOUT_PARALLEL_EXPRESSIONS`). A transport can set
    an optional ``parallel_expressions`` attribute to ask for fewer, or
    leave it ``None`` (or unset) for that default.
    """

    async def evaluate_client_relevance_fanout(
        self, client_relevance: str, *, timeout_s: float = 30.0
    ) -> list[ClientRelevanceResult]:
        """One result per endpoint, never raising; at least one result."""
        ...


@runtime_checkable
class BatchTransport(Protocol):
    """Optional: many expressions through one qna process.

    Each expression run on its own pays for a qna start and, for a
    container, an exec; on one qna's stdin they cost almost nothing (see
    :mod:`.batch`). Returning ``None`` -- never raising -- hands the chunk
    back to the orchestrator to evaluate one expression at a time, which is
    the right answer to anything wrong with the run as a whole (a timeout, a
    nonzero exit, a build too new to start, output that does not line up):
    the single path already reports each of those in its own words.
    """

    async def evaluate_client_relevance_batch(
        self,
        expressions: Sequence[str],
        *,
        qna: ResolvedQna | None = None,
        timeout_s: float = 30.0,
    ) -> list[ClientRelevanceResult] | None:
        """One result per expression, in order; or ``None`` to fall back.

        ``timeout_s`` bounds the whole run, not each expression.
        """
        ...


ARCH_PROBE_RELEVANCE = "architecture of the operating system"
"""Client relevance used by :func:`probe_arch_via_relevance`."""


async def probe_arch_via_relevance(
    evaluate: Callable[..., Awaitable[ClientRelevanceResult]], *, timeout_s: float = 30.0
) -> str:
    """Probe a target's architecture using relevance itself.

    For transports with no other side channel to ask -- unlike
    :class:`~.local.TransportLocal` (``sys.platform``) or
    :class:`~.ssh.TransportSSH` (``uname -m``),
    :class:`~.online_evaluator.TransportOnlineEvaluator` has nothing but
    relevance itself to ask, so the probe *is* an ordinary evaluation. ``evaluate`` is the
    transport's own bound ``evaluate_client_relevance``.

    Raises on any failure -- a relevance error, a transport error, or an
    empty answer -- so orchestrate.py's generic arch-probe machinery (which
    calls this through each transport's ``resolve_arch``) falls back to its
    usual ``"x86_64"`` default instead of this helper inventing its own.
    """
    result = await evaluate(ARCH_PROBE_RELEVANCE, timeout_s=timeout_s)
    if result.error_kind is not None:
        raise RuntimeError(f"arch probe failed: {result.error}")
    if not result.answers:
        raise RuntimeError("arch probe returned no answer")
    return result.answers[0]


__all__ = [
    "ARCH_PROBE_RELEVANCE",
    "BatchTransport",
    "FanOutTransport",
    "Transport",
    "probe_arch_via_relevance",
]
