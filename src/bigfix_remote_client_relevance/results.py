"""Client-relevance result types and the qna output parser.

qna reports on four line-oriented channels:

===========  ==========================================================
``A:``       an answer
``E:``       an error in the client relevance itself
``I:``       the result type, emitted when ``-showtypes`` is passed
``T:``       time taken, emitted when ``-t`` is passed
===========  ==========================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# error_kind values. None means success; these classify every failure mode so
# callers (and the CLI's exit codes) can react without string matching.
ERROR_KIND_RELEVANCE = "relevance"
"""The client relevance was evaluated and qna reported an ``E:`` error."""

ERROR_KIND_QNA = "qna"
"""qna itself failed: nonzero exit, or output with no recognizable channels."""

ERROR_KIND_BOOTSTRAP = "bootstrap"
"""Provisioning failed: no qna binary, push/extract failure, missing prereq."""

ERROR_KIND_TRANSPORT = "transport"
"""Reaching the target failed: connect, auth, or timeout."""

ERROR_KIND_RESOLVE = "resolve"
"""A qna version spec could not be resolved to a full version."""

ERROR_KINDS: tuple[str, ...] = (
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_QNA,
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_TRANSPORT,
    ERROR_KIND_RESOLVE,
)
"""Every non-``None`` ``error_kind``, for callers that need to enumerate them."""

# A resolved version is four dot-separated numbers, e.g. 11.0.6.137 — never a
# spec like "11.0".
_FULL_VERSION = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

_LINE_SPLIT = re.compile(r"\r\n|\r|\n")

_ANSWER_PREFIX = "A: "
_ERROR_PREFIX = "E: "
_TYPE_PREFIX = "I: "
_TIME_PREFIX = "T: "
_ECHO_PREFIX = "Q: "


@dataclass(frozen=True)
class ParsedQnaOutput:
    """The four qna channels, separated. Pure data — no I/O, no interpretation."""

    answers: list[str] = field(default_factory=list)
    answer_types: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    qna_time: str | None = None

    @property
    def has_recognizable_output(self) -> bool:
        """True when qna spoke on any known channel.

        Distinguishes a relevance that legitimately answered nothing (valid
        transcript, zero answers) from qna emitting something unparsable.
        """
        return bool(self.answers or self.answer_types or self.errors or self.qna_time)


@dataclass
class ClientRelevanceResult:
    """One evaluation of one client-relevance expression on one target.

    Produced per (target x qna version). This is the shape the CLI's ``--json``
    emits and the future MCP tool returns, so field names are a public contract.
    """

    host: str
    """``"local"``, an SSH host, or ``"container:<image>@<arch>"``."""

    transport: str
    """``"local"`` | ``"ssh"`` | ``"container"`` | ``"fastquery"`` |
    ``"online_evaluator"``."""

    client_relevance: str
    """The expression as given by the caller, before any stdin normalization."""

    answers: list[str] = field(default_factory=list)
    answer_types: list[str] = field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None
    raw_qna_output: str = ""
    """Full qna stdout, kept for debugging and for agents to self-correct on.

    A result field only — the library never relays it to its own stdout.
    """

    qna_path: str = ""
    qna_version: str | None = None
    qna_time: str | None = None
    """qna's own ``T:`` timing, distinct from caller-measured ``elapsed_ms``."""

    elapsed_ms: int = 0
    exit_code: int = 0

    platform: str | None = None
    """The :data:`KNOWN_TARGETS` key this run actually used, when known --
    from an explicit ``Target.platform``, from a fresh probe, or (after a
    bootstrap failure with an explicit platform set) from a corrective
    re-probe that ignored it. ``None`` when no platform concept applies
    (fastquery) or none was ever resolved. This is what the CLI's
    ``--update-inventory`` writes back to ``remote_clients.toml``."""

    arch: str | None = None
    """The architecture this run actually used, when known -- an explicit
    ``Target.arch`` (always the case for a container, since its arch is a
    per-run choice never inferred), or a fresh probe on an ssh/local target.
    ``None`` for fastquery, or when never resolved. This is what the CLI's
    ``--update-inventory`` writes back to ``remote_clients.toml``, alongside
    ``platform``."""

    @property
    def ok(self) -> bool:
        return self.error_kind is None


@dataclass(frozen=True)
class ResolvedQna:
    """A fully-resolved qna version plus its cached artifact.

    Produced by the orchestration layer and consumed by transports. Version
    specs (``"11.0"``) are resolved upstream and never reach a transport, which
    keeps every transport offline-testable.
    """

    version: str
    artifact_path: Path | None

    def __post_init__(self) -> None:
        if not _FULL_VERSION.match(self.version):
            raise ValueError(
                f"ResolvedQna needs a full four-part version, got {self.version!r}. "
                "Version specs must be resolved before reaching a transport."
            )


def parse_qna_output(raw: str) -> ParsedQnaOutput:
    """Split raw qna output into its channels.

    Prefixes are anchored at the start of a line, so an ``A:`` appearing inside
    an answer is content rather than a new channel. Only the first prefix is
    stripped, preserving any later occurrence in the answer text.
    """
    answers: list[str] = []
    answer_types: list[str] = []
    errors: list[str] = []
    qna_time: str | None = None

    for raw_line in _LINE_SPLIT.split(raw):
        line = raw_line
        # Interactive mode echoes the prompt, so an answer can arrive as
        # "Q: A: <answer>". Strip one echo prefix before matching channels; a
        # plain question echo simply matches nothing afterwards.
        line = line.removeprefix(_ECHO_PREFIX)

        if line.startswith(_ANSWER_PREFIX):
            answers.append(line[len(_ANSWER_PREFIX) :])
        elif line.startswith(_ERROR_PREFIX):
            errors.append(line[len(_ERROR_PREFIX) :])
        elif line.startswith(_TYPE_PREFIX):
            answer_types.append(line[len(_TYPE_PREFIX) :])
        elif line.startswith(_TIME_PREFIX) and qna_time is None:
            qna_time = line[len(_TIME_PREFIX) :]

    return ParsedQnaOutput(
        answers=answers,
        answer_types=answer_types,
        errors=errors,
        qna_time=qna_time,
    )


__all__ = [
    "ERROR_KINDS",
    "ERROR_KIND_BOOTSTRAP",
    "ERROR_KIND_QNA",
    "ERROR_KIND_RELEVANCE",
    "ERROR_KIND_RESOLVE",
    "ERROR_KIND_TRANSPORT",
    "ClientRelevanceResult",
    "ParsedQnaOutput",
    "ResolvedQna",
    "parse_qna_output",
]
