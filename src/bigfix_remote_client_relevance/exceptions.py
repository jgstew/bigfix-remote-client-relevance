"""The one exception base every error in this package derives from.

The fan-out entry points in :mod:`~bigfix_remote_client_relevance.orchestrate`
never raise for a target failure — a failed host comes back as a
:class:`~bigfix_remote_client_relevance.results.ClientRelevanceResult` with
``error_kind`` set. Exceptions belong to the *setup* path instead: loading an
inventory, resolving a version spec, priming the artifact cache.

Callers wrapping that path — an MCP server's tool handler, most obviously —
want a single ``except`` clause rather than eight imports. Each concrete
exception keeps its own class and its own module; this base is purely additive.
"""

from __future__ import annotations


class BigFixRelevanceError(Exception):
    """Base for every exception raised by this package."""


__all__ = ["BigFixRelevanceError"]
