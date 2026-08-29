"""The JSON wire shape of a result, and the schema that describes it.

:class:`~bigfix_remote_client_relevance.results.ClientRelevanceResult` field
names are already a public contract; this module makes the *serialized* form
one too, so the CLI's ``--json``/``--jsonl`` output and an MCP server's
``structuredContent`` are the same document produced by the same code rather
than two independent ``dataclasses.asdict`` calls that drift.

Over the dataclass it adds three things a consumer would otherwise write:

* a stable, explicit key order;
* the derived ``ok`` field, so nobody has to know that ``ok`` means
  ``error_kind is None``;
* ``max_raw_output``, a cap on ``raw_qna_output``. That field is deliberately
  unbounded on the dataclass — it is what lets an agent read qna's own words and
  self-correct — but an MCP tool result is charged in tokens, so a server needs
  a cap it did not have to implement.

**Evolution policy.** Within a major version this shape only ever grows: fields
are added, never removed or retyped, and :data:`SCHEMA_VERSION` bumps when they
are. The schema therefore does not set ``additionalProperties: false`` — a
consumer validating against an older copy of it must keep working against a
newer emitter.

Stdlib only, by design: an MCP server importing this must not inherit a
validation or serialization dependency from us.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

from bigfix_remote_client_relevance.results import ClientRelevanceResult


class ResultPayload(TypedDict):
    """The serialized form of a :class:`ClientRelevanceResult`.

    A ``TypedDict`` rather than a bare ``dict[str, object]`` so a type-checked
    consumer gets real types out of an index, instead of having to narrow every
    field it touches. JSON-compatible: this is a plain dict at runtime.
    """

    host: str
    transport: str
    platform: str | None
    client_relevance: str
    ok: bool
    answers: list[str]
    answer_types: list[str]
    error: str | None
    error_kind: str | None
    exit_code: int
    qna_version: str | None
    qna_path: str
    qna_time: str | None
    elapsed_ms: int
    raw_qna_output: str


SCHEMA_VERSION = "1.0"
"""Bumped on any additive change to the result payload. See the module docstring."""

SCHEMA_ID = (
    "https://github.com/jgstew/bigfix-remote-client-relevance/schema/client-relevance-result.json"
)

# The emitted key order. Identity and outcome first, then the answer, then
# provenance, with the potentially large raw transcript last so a truncated
# read of a JSONL line still carries everything that matters.
_KEY_ORDER = (
    "host",
    "transport",
    "platform",
    "client_relevance",
    "ok",
    "answers",
    "answer_types",
    "error",
    "error_kind",
    "exit_code",
    "qna_version",
    "qna_path",
    "qna_time",
    "elapsed_ms",
    "raw_qna_output",
)


def _cap(raw: str, limit: int) -> str:
    """Keep the first ``limit`` characters, then say what was dropped.

    The marker is appended after the kept prefix, so the returned string is
    slightly longer than ``limit`` -- the cap bounds the payload, not the
    field.
    """
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}\n[truncated: kept {limit} of {len(raw)} characters]"


def result_to_dict(
    result: ClientRelevanceResult,
    *,
    max_raw_output: int | None = None,
) -> ResultPayload:
    """Serialize one result to its JSON wire shape.

    ``max_raw_output`` caps ``raw_qna_output`` at that many characters, adding a
    marker naming the original length. ``None`` (the default) keeps it whole.
    The result itself is never mutated.
    """
    if max_raw_output is not None and max_raw_output < 0:
        raise ValueError(f"max_raw_output must be >= 0 or None, got {max_raw_output}")

    raw = result.raw_qna_output
    if max_raw_output is not None:
        raw = _cap(raw, max_raw_output)

    payload: ResultPayload = {
        "host": result.host,
        "transport": result.transport,
        "platform": result.platform,
        "client_relevance": result.client_relevance,
        "ok": result.ok,
        # Copied, not aliased: a consumer mutating the payload must not reach
        # back into the result it came from.
        "answers": list(result.answers),
        "answer_types": list(result.answer_types),
        "error": result.error,
        "error_kind": result.error_kind,
        "exit_code": result.exit_code,
        "qna_version": result.qna_version,
        "qna_path": result.qna_path,
        "qna_time": result.qna_time,
        "elapsed_ms": result.elapsed_ms,
        "raw_qna_output": raw,
    }
    return payload


def results_to_dicts(
    results: Sequence[ClientRelevanceResult],
    *,
    max_raw_output: int | None = None,
) -> list[ResultPayload]:
    """Serialize a fan-out, preserving the order it was given in."""
    return [result_to_dict(result, max_raw_output=max_raw_output) for result in results]


_STRING_OR_NULL = ["string", "null"]

RESULT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SCHEMA_ID,
    "x-schema-version": SCHEMA_VERSION,
    "title": "ClientRelevanceResult",
    "description": (
        "One evaluation of one client-relevance expression on one target, for one qna "
        "version. Emitted by the CLI's --json and --jsonl output and by "
        "bigfix_remote_client_relevance.serialize.result_to_dict."
    ),
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "description": '"local", an SSH host, or "container:<image>@<arch>".',
        },
        "transport": {
            "type": "string",
            "enum": ["local", "ssh", "container", "fastquery"],
        },
        "platform": {
            "type": _STRING_OR_NULL,
            "description": (
                "The known-target key this run used, when one applies and was resolved."
            ),
        },
        "client_relevance": {
            "type": "string",
            "description": "The expression as given by the caller.",
        },
        "ok": {
            "type": "boolean",
            "description": "True when error_kind is null. Derived, not stored.",
        },
        "answers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "qna's A: channel. Empty is a valid answer for a plural inspector.",
        },
        "answer_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": 'qna\'s I: channel, e.g. "singular string".',
        },
        "error": {
            "type": _STRING_OR_NULL,
            "description": "Human-readable failure detail; null on success.",
        },
        "error_kind": {
            "type": _STRING_OR_NULL,
            "enum": ["relevance", "qna", "bootstrap", "transport", "resolve", None],
            "description": (
                "How it failed, or null on success. relevance = the expression itself was "
                "wrong (agents can self-correct on this one); qna = the binary failed; "
                "bootstrap = provisioning failed; transport = the target was unreachable; "
                "resolve = the version spec could not be resolved."
            ),
        },
        "exit_code": {"type": "integer"},
        "qna_version": {
            "type": _STRING_OR_NULL,
            "description": "The full four-part version actually evaluated, e.g. 11.0.6.137.",
        },
        "qna_path": {"type": "string", "description": "Path to the qna binary on the target."},
        "qna_time": {
            "type": _STRING_OR_NULL,
            "description": "qna's own T: timing, distinct from elapsed_ms.",
        },
        "elapsed_ms": {
            "type": "integer",
            "description": "Wall-clock time measured by the controller.",
        },
        "raw_qna_output": {
            "type": "string",
            "description": (
                "Full qna stdout, for debugging and for agents to self-correct on. May be "
                "truncated with a trailing marker when the producer applied a cap."
            ),
        },
    },
    "required": list(_KEY_ORDER),
}


__all__ = [
    "RESULT_JSON_SCHEMA",
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "ResultPayload",
    "result_to_dict",
    "results_to_dicts",
]
