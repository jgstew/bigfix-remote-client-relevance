"""Evaluate BigFix client relevance on remote endpoints and in containers.

This package deals only in **client relevance** — the dialect ``qna`` and the
BES client evaluate on an endpoint. Session relevance (the ``bes-*`` object
model queried through the root server's REST ``/api/query``) is a different
dialect and is out of scope here.

The library logs through :mod:`logging` and never writes to stdout, which is
reserved for the CLI's result payload and a stdio MCP server's JSON-RPC
stream. Applications configure handlers; this package attaches only a
``NullHandler``.

Everything an embedding application needs is re-exported here, so importing
from a submodule is never required:

* :func:`evaluate_client_relevance` and
  :func:`evaluate_client_relevance_stream` — the fan-out, batched or in
  completion order. Neither raises for a target failure; a bad host comes back
  as a :class:`ClientRelevanceResult` with ``error_kind`` set.
* :func:`count_work` — the ``targets x versions`` total, i.e. the denominator
  for a progress indicator over the streaming form.
* :func:`result_to_dict` / :func:`results_to_dicts` and
  :data:`RESULT_JSON_SCHEMA` — the JSON wire shape and its schema.
* :func:`format_result` / :func:`format_results` — the same human-readable text
  the CLI prints, without the CLI's dependencies.
* :class:`BigFixRelevanceError` — one base for every exception the setup path
  (inventory loading, version resolution, artifact caching) can raise.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from bigfix_remote_client_relevance.exceptions import BigFixRelevanceError
from bigfix_remote_client_relevance.inventory import InventoryError, load_inventory
from bigfix_remote_client_relevance.orchestrate import (
    EXIT_OK,
    EXIT_QNA,
    EXIT_RELEVANCE,
    EXIT_RESOLVE,
    EXIT_TRANSPORT,
    Target,
    count_work,
    evaluate_client_relevance,
    evaluate_client_relevance_stream,
    worst_exit_code,
)
from bigfix_remote_client_relevance.qna_paths import find_qna_path
from bigfix_remote_client_relevance.render import format_result, format_results
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_RESOLVE,
    ERROR_KIND_TRANSPORT,
    ERROR_KINDS,
    ClientRelevanceResult,
    ParsedQnaOutput,
    ResolvedQna,
    parse_qna_output,
)
from bigfix_remote_client_relevance.serialize import (
    RESULT_JSON_SCHEMA,
    SCHEMA_VERSION,
    ResultPayload,
    result_to_dict,
    results_to_dicts,
)
from bigfix_remote_client_relevance.transports import Transport
from bigfix_remote_client_relevance.transports.container import TransportContainer
from bigfix_remote_client_relevance.transports.fastquery import TransportFastQuery
from bigfix_remote_client_relevance.transports.local import TransportLocal
from bigfix_remote_client_relevance.transports.ssh import TransportSSH

try:
    __version__ = _version("bigfix-remote-client-relevance")
except PackageNotFoundError:  # pragma: no cover - only when run from a source tree
    __version__ = "0.0.0+unknown"

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "ERROR_KINDS",
    "ERROR_KIND_BOOTSTRAP",
    "ERROR_KIND_QNA",
    "ERROR_KIND_RELEVANCE",
    "ERROR_KIND_RESOLVE",
    "ERROR_KIND_TRANSPORT",
    "EXIT_OK",
    "EXIT_QNA",
    "EXIT_RELEVANCE",
    "EXIT_RESOLVE",
    "EXIT_TRANSPORT",
    "RESULT_JSON_SCHEMA",
    "SCHEMA_VERSION",
    "BigFixRelevanceError",
    "ClientRelevanceResult",
    "InventoryError",
    "ParsedQnaOutput",
    "ResolvedQna",
    "ResultPayload",
    "Target",
    "Transport",
    "TransportContainer",
    "TransportFastQuery",
    "TransportLocal",
    "TransportSSH",
    "count_work",
    "evaluate_client_relevance",
    "evaluate_client_relevance_stream",
    "find_qna_path",
    "format_result",
    "format_results",
    "load_inventory",
    "parse_qna_output",
    "result_to_dict",
    "results_to_dicts",
    "worst_exit_code",
]
