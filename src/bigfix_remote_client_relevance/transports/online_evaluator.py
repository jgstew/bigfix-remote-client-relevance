"""Evaluate client relevance via a hosted, qna-style HTTP relevance API.

Reverse-engineered from BigFix's own "Online Evaluator"
(<https://developer.bigfix.com/relevance/evaluate/>), whose JS bundle
(``initEvaluator`` / ``evaluateRelevance``) does exactly this:

* ``POST {base_url}/api/relevance/evaluate``
* Body: ``{"relevance": "<expression>"}``, ``Content-Type: application/json``
* Response (200, JSON): ``{"answers": [...], "errors": [...], "time": <int ms>,
  "type": "<string>"}`` -- confirmed live, e.g.
  ``{"answers":["Linux Red Hat Enterprise Linux 8.1"],"errors":[],"time":255,"type":"string"}``
  on success, and ``{"answers":[],"errors":["This expression contained a
  character which is not allowed."],"time":0,"type":""}`` on a relevance error.

The docs text says "the evaluation is run on a Linux RHEL system" -- so this is
a backend service that itself runs (or embeds) ``qna`` on one fixed box and
reports the outcome as JSON, rather than qna's own ``A:``/``E:``/``I:``/``T:``
lines. Functionally the same operation :class:`~...transports.local.TransportLocal`
performs, just hosted.

**This talks to a third-party HTTP endpoint.** The ``developer.bigfix.com``
API this was reverse-engineered from is undocumented and unsupported -- it is
the backend of a web page, not a published API -- and can change or disappear
without notice. It has no visible authentication and no visible rate limit,
so do not hammer it in a loop. Whatever ``base_url`` is configured, the
client-relevance expression and its answers leave this machine over the
network -- the same consideration :class:`~...transports.ssh.TransportSSH`
already carries, just to a host you may not operate. ``base_url`` therefore has
no default: pointing this at a live service is an opt-in choice made by the
caller, never an accident.

API boundary: the wire body key is ``relevance`` (matching qna's own CLI
vocabulary and the BigFix REST API's ``Relevance``); the internal name stays
``client_relevance``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urljoin, urlparse

import requests

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ParsedQnaOutput,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports import probe_arch_via_relevance
from bigfix_remote_client_relevance.transports.local import classify_qna_outcome

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "online_evaluator"

_EVALUATE_PATH = "/api/relevance/evaluate"

# The page's own error handling singles this status out by number
# ("Online evaluator is not available (502)") as a known transient failure of
# the backend, distinct from every other status it just relays verbatim.
_RETRYABLE_STATUS = 502

_RETRY_DELAY_S = 0.5


class TransportOnlineEvaluator:
    """Evaluates client relevance against a hosted qna-style HTTP API.

    Unlike :class:`~...transports.local.TransportLocal`,
    :class:`~...transports.ssh.TransportSSH` and
    :class:`~...transports.container.TransportContainer`, this transport never
    runs qna itself and never provisions anything -- it only speaks the small
    JSON contract described in the module docstring to whatever ``base_url``
    is given.
    """

    def __init__(
        self,
        base_url: str,
        *,
        host: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = 1,
    ) -> None:
        """
        Args:
            base_url: Origin of the service, e.g. ``"https://developer.bigfix.com"``.
                Required -- there is deliberately no default (see module
                docstring). ``/api/relevance/evaluate`` is appended to it.
            host: :class:`ClientRelevanceResult.host` value; defaults to
                ``base_url``'s hostname.
            session: Injected for tests; defaults to a fresh
                :class:`requests.Session`.
            max_retries: Extra attempts on an HTTP 502 only (see
                :data:`_RETRYABLE_STATUS`). Every other failure -- other
                status codes, timeouts, connection errors -- is reported
                immediately, unretried.
        """
        if not base_url:
            raise ValueError("TransportOnlineEvaluator needs a base_url")
        self._base_url = base_url
        self._url = urljoin(base_url.rstrip("/") + "/", _EVALUATE_PATH.lstrip("/"))
        self._host = host or urlparse(base_url).hostname or base_url
        self._session = session or requests.Session()
        self._max_retries = max_retries

    async def resolve_arch(self, *, timeout_s: float = 30.0) -> str:
        """Probe this service's own architecture via relevance.

        Unlike :class:`~.local.TransportLocal` (``sys.platform``) or
        :class:`~.ssh.TransportSSH` (``uname -m``), an online evaluator
        exposes no side channel to ask -- the probe *is* an ordinary
        evaluation, sent through the same :meth:`evaluate_client_relevance`
        every other expression goes through. orchestrate.py calls this
        automatically (via duck-typed ``resolve_arch`` detection) whenever a
        target's ``arch`` is unset, and writes the result back into
        ``remote_clients.toml`` when running with ``--inventory`` --
        `--update-inventory` (on by default) -- so a given service is probed
        at most once: the next run finds ``arch`` already set and skips this
        entirely.
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

        def _result(**overrides: object) -> ClientRelevanceResult:
            base: dict[str, object] = {
                "host": self._host,
                "transport": TRANSPORT_NAME,
                "client_relevance": client_relevance,
                "qna_path": self._url,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            base.update(overrides)
            return ClientRelevanceResult(**base)  # type: ignore[arg-type]

        if qna is not None:
            return _result(
                error=(
                    "the online evaluator transport cannot pin a qna version: it "
                    f"evaluates on a fixed, remotely-managed environment, so version "
                    f"{qna.version} cannot be requested. Use the ssh, container, or "
                    "local transport to pin a version."
                ),
                error_kind=ERROR_KIND_BOOTSTRAP,
            )

        if qna_path is not None:
            logger.debug(
                "qna_path=%r has no effect on %s: there is no local binary to select",
                qna_path,
                TRANSPORT_NAME,
            )

        # API boundary: the wire body key is "relevance"; internal name stays
        # `client_relevance`.
        payload = {"relevance": client_relevance}

        attempts = self._max_retries + 1
        response: requests.Response | None = None
        for attempt in range(attempts):
            try:
                response = await asyncio.to_thread(
                    self._session.post, self._url, json=payload, timeout=timeout_s
                )
            except requests.RequestException as exc:
                return _result(
                    error=f"could not reach {self._url}: {exc}",
                    error_kind=ERROR_KIND_TRANSPORT,
                )

            if response.status_code == _RETRYABLE_STATUS and attempt + 1 < attempts:
                logger.warning(
                    "online evaluator returned 502 (not available); retrying (%d/%d)",
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(_RETRY_DELAY_S)
                continue
            break

        assert response is not None  # loop always runs at least once

        if response.status_code != 200:
            error = f"online evaluator is not available ({response.status_code})"
            if response.status_code != _RETRYABLE_STATUS:
                detail = response.text.strip()
                if detail:
                    error = f"{error}: {detail}"
            return _result(
                error=error,
                raw_qna_output=response.text,
                error_kind=ERROR_KIND_TRANSPORT,
            )

        try:
            body = response.json()
        except ValueError as exc:
            return _result(
                error=f"could not parse the online evaluator's response: {exc}",
                raw_qna_output=response.text,
                error_kind=ERROR_KIND_TRANSPORT,
            )

        try:
            answers = [str(a) for a in body["answers"]]
            errors = [str(e) for e in body["errors"]]
            time_ms = body.get("time")
            result_type = body.get("type") or None
        except (KeyError, TypeError) as exc:
            return _result(
                error=f"online evaluator response missing an expected field: {exc}",
                raw_qna_output=response.text,
                error_kind=ERROR_KIND_TRANSPORT,
            )

        # The service reports one aggregate `type` for the whole answer set,
        # unlike qna's own per-answer `I:` lines -- broadcast it across every
        # answer so `answer_types` stays the same shape every transport uses.
        answer_types = [result_type] * len(answers) if result_type else []

        parsed = ParsedQnaOutput(
            answers=answers,
            answer_types=answer_types,
            errors=errors,
            qna_time=str(time_ms) if time_ms is not None else None,
        )
        outcome_error, error_kind = classify_qna_outcome(parsed, exit_code=0, stderr="")

        return _result(
            answers=parsed.answers,
            answer_types=parsed.answer_types,
            error=outcome_error,
            error_kind=error_kind,
            raw_qna_output=response.text,
            qna_time=parsed.qna_time,
            exit_code=0,
        )


__all__ = ["TransportOnlineEvaluator"]
