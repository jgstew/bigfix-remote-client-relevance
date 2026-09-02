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

    ``container:<image>@<arch>`` already says how it's reached; a bare ssh
    host like ``192.168.4.115`` doesn't, and an inventory can mix an ssh and
    a fastquery entry for the same address. This is display-only:
    ``result.host`` itself stays untouched, since the CLI's inventory
    writeback matches it against the inventory's table names verbatim.

    ssh/fastquery/local build a fuller, platform-including header in
    :func:`label` instead of stopping here -- see its docstring.
    """
    if result.transport in ("ssh", "fastquery") and not result.host.startswith(
        f"{result.transport}:"
    ):
        return f"{result.transport}:{result.host}"
    return result.host


# Long enough to tell two inspectors apart, short enough that a section header
# stays one terminal line next to the host and version it already carries.
_LABEL_EXPRESSION_CHARS = 60

# Transports whose header gets an `@arch` suffix when the arch is known.
# Container already bakes its arch into `result.host` itself
# (`container:<image>@<arch>`), so it is deliberately not repeated here.
# fastquery is also left out: nothing currently populates its arch, and
# nothing asked for it to display one.
_ARCH_SUFFIXED_TRANSPORTS = frozenset({"local", "ssh", "online_evaluator"})


def label(result: ClientRelevanceResult, *, with_expression: bool = False) -> str:
    """Full header text: ``transport:platform:host``, then the qna version --
    e.g. ``ssh:windows:192.168.4.115 (qna 11.0.6.137)``, or
    ``local:macos:this-mac-builtin`` for a local inventory entry -- echoing
    the ``container:<image>@<arch>`` shape's colon-separated qualifier.

    ssh, fastquery, and local all get this shape: their host strings alone
    don't say what's on the other end -- an inventory full of bare IPs and
    SSH aliases (or local entries with names like ``this-mac-builtin``) gives
    no hint which is Windows, macOS, or Linux, and for local a
    ``qna_version`` fan-out can turn one target into several results that
    would otherwise all print the same bare, indistinguishable header. The
    transport always leads, and the platform sits between it and the host
    (not after, like the ``container:<image>@<arch>`` shape puts its arch)
    so every header still says ``local``/``ssh``/``fastquery`` even when the
    platform is unknown. A container image already names its own OS, so
    repeating it there would just be noise -- container keeps its own shape
    entirely.

    The bare literal ``"local"`` host from a nameless, ad hoc invocation (the
    CLI's ``--local`` flag) is dropped rather than doubled into
    ``local:macos:local``, since it names nothing an inventory entry would.
    An already-transport-qualified host (see :func:`display_host`) is not
    double-prefixed.

    ``local``, ``ssh``, and ``online_evaluator`` also get an ``@arch`` suffix
    when the arch is known -- ``local:macos:this-mac@arm64``,
    ``ssh:windows:0.0.0.0@x86_64``, or the bare ``web-eval-rhel@x86_64`` for
    online_evaluator (which isn't in the transport:platform:host group above
    and keeps its plain host). Echoes the same ``@arch`` shape
    ``container:<image>@<arch>`` already uses, so container is deliberately
    left out here -- its arch is already part of ``result.host``, not added
    by this function.
    """
    if result.transport in ("ssh", "fastquery", "local"):
        prefix = f"{result.transport}:"
        bare_host = result.host.removeprefix(prefix)
        parts = [result.transport]
        if result.platform:
            parts.append(result.platform)
        if not (result.transport == "local" and bare_host == "local"):
            parts.append(bare_host)
        text = ":".join(parts)
    else:
        text = display_host(result)
    if result.transport in _ARCH_SUFFIXED_TRANSPORTS and result.arch:
        text = f"{text}@{result.arch}"
    if result.qna_version:
        text = f"{text} (qna {result.qna_version})"
    if with_expression:
        # A batch puts several results under one host and version; only the
        # expression tells them apart. Off by default, since the single-
        # expression case would just repeat what the caller already typed.
        expression = " ".join(result.client_relevance.split())
        if len(expression) > _LABEL_EXPRESSION_CHARS:
            expression = expression[: _LABEL_EXPRESSION_CHARS - 1] + "\u2026"
        text = f"{text} {expression}"
    return text


def format_result(
    result: ClientRelevanceResult, *, labelled: bool = False, with_expression: bool = False
) -> str:
    """One result's section. Shared by the batch and streaming renderers, so
    the two can never drift into printing the same result differently."""
    lines: list[str] = []
    if labelled:
        lines.append(f"== {label(result, with_expression=with_expression)}")
    lines.extend(result.answers)
    if result.error:
        # Errors are rendered inline as part of a labelled section; the CLI's
        # separate summary on stderr is what a human reads.
        lines.append(f"!! {result.error_kind}: {result.error}")
    return "\n".join(lines)


def format_results(results: Sequence[ClientRelevanceResult]) -> str:
    """Answers for a single result; host-labelled sections for a fan-out.

    A batch puts several results under the same host and version, so the
    headers name the expression too -- but only when there is more than one in
    play. Repeating the single expression the caller just typed, once per host,
    would be noise.
    """
    multiple = len(results) > 1
    several_expressions = len({result.client_relevance for result in results}) > 1
    return "\n".join(
        format_result(result, labelled=multiple, with_expression=several_expressions)
        for result in results
    )


__all__ = ["display_host", "format_result", "format_results", "label"]
