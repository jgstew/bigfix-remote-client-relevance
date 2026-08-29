"""Human-readable rendering of results.

The same text the CLI prints, available without importing the CLI -- an MCP
server wants it for a tool result's ``content`` block, and should not have to
take on ``typer`` to get it.

Presentation only: nothing here does I/O, and (like everything outside
``cli.py``) nothing here writes to stdout. Renderers that are genuinely
CLI-specific -- the ``--diff`` grouping, the stderr failure summary -- stay in
``cli.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

from bigfix_remote_client_relevance.results import ClientRelevanceResult


def display_host(result: ClientRelevanceResult) -> str:
    """The host as shown in headers -- not necessarily ``result.host`` as-is.

    ``local`` and ``container:<image>@<arch>`` already say how they're
    reached; a bare ssh host like ``192.168.4.115`` doesn't, and an
    inventory can mix an ssh and a fastquery entry for the same address.
    This is display-only: ``result.host`` itself stays untouched, since the
    CLI's inventory writeback matches it against the inventory's table names
    verbatim.
    """
    if result.transport in ("ssh", "fastquery") and not result.host.startswith(
        f"{result.transport}:"
    ):
        return f"{result.transport}:{result.host}"
    return result.host


def label(result: ClientRelevanceResult) -> str:
    """Full header text: the display host, its platform when known, and the
    qna version -- e.g. ``ssh:192.168.4.115:windows (qna 11.0.6.137)``, echoing
    the ``container:<image>@<arch>`` shape's colon-separated qualifier.

    Platform is shown only for ssh/fastquery, the two transports whose host
    string alone doesn't say what's on the other end (an inventory full of
    bare IPs and SSH aliases gives no hint which is Windows, macOS, or
    Linux). ``local`` is always this machine and a container image already
    names its own OS, so both would just be repeating themselves.
    """
    text = display_host(result)
    if result.transport in ("ssh", "fastquery") and result.platform:
        text = f"{text}:{result.platform}"
    if result.qna_version:
        text = f"{text} (qna {result.qna_version})"
    return text


def format_result(result: ClientRelevanceResult, *, labelled: bool = False) -> str:
    """One result's section. Shared by the batch and streaming renderers, so
    the two can never drift into printing the same result differently."""
    lines: list[str] = []
    if labelled:
        lines.append(f"== {label(result)}")
    lines.extend(result.answers)
    if result.error:
        # Errors are rendered inline as part of a labelled section; the CLI's
        # separate summary on stderr is what a human reads.
        lines.append(f"!! {result.error_kind}: {result.error}")
    return "\n".join(lines)


def format_results(results: Sequence[ClientRelevanceResult]) -> str:
    """Answers for a single result; host-labelled sections for a fan-out."""
    multiple = len(results) > 1
    return "\n".join(format_result(result, labelled=multiple) for result in results)


__all__ = ["display_host", "format_result", "format_results", "label"]
