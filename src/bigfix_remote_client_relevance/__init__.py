"""Evaluate BigFix client relevance on remote endpoints and in containers.

This package deals only in **client relevance** — the dialect ``qna`` and the
BES client evaluate on an endpoint. Session relevance (the ``bes-*`` object
model queried through the root server's REST ``/api/query``) is a different
dialect and is out of scope here.

The library logs through :mod:`logging` and never writes to stdout, which is
reserved for the CLI's result payload and a future stdio MCP server's JSON-RPC
stream. Applications configure handlers; this package attaches only a
``NullHandler``.
"""

from __future__ import annotations

import logging

from bigfix_remote_client_relevance.orchestrate import (
    Target,
    evaluate_client_relevance,
    evaluate_client_relevance_stream,
    worst_exit_code,
)
from bigfix_remote_client_relevance.qna_paths import find_qna_path
from bigfix_remote_client_relevance.results import (
    ClientRelevanceResult,
    ParsedQnaOutput,
    ResolvedQna,
    parse_qna_output,
)
from bigfix_remote_client_relevance.transports import Transport
from bigfix_remote_client_relevance.transports.container import TransportContainer
from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery
from bigfix_remote_client_relevance.transports.local import TransportLocal
from bigfix_remote_client_relevance.transports.ssh import TransportSSH

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ClientRelevanceResult",
    "ParsedQnaOutput",
    "ResolvedQna",
    "Target",
    "Transport",
    "TransportContainer",
    "TransportFastQuery",
    "TransportLocal",
    "TransportSSH",
    "evaluate_client_relevance",
    "evaluate_client_relevance_stream",
    "find_qna_path",
    "parse_qna_output",
    "worst_exit_code",
]
