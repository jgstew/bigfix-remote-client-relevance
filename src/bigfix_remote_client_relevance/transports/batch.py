"""Many client-relevance expressions through one qna process.

qna's stdin mode reads one question per line and answers each in its own
block, in order, without echoing the question (the macOS agent's QnA
prints no ``Q:`` prompts at all, only the blank lines)::

    Q: A: 11.0.7.61
    T: 10650
    I: version

    Q: E: The operator "this" is not defined.

    Q:

An error does not stop the run, a parse error has no ``T:`` line, and the
last line is one more, empty prompt. So answers can only be matched to
questions by position -- which one stray ``Q:`` line (an answer spilling a
newline) would silently shift. A sentinel question after every expression
makes that checkable: every other block must answer exactly the sentinel, or
the split is refused and the caller evaluates one expression at a time
instead. Refusing costs time; misattributing an answer costs correctness.

Measured on a native container (issue #9): 368 expressions on one qna stdin
took 70 ms, against ~190 ms *each* as separate execs.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence

from bigfix_remote_client_relevance.results import ClientRelevanceResult, parse_qna_output
from bigfix_remote_client_relevance.transports.local import (
    classify_qna_outcome,
    normalize_stdin_payload,
)

SENTINEL_PREFIX = "bfrcr-batch-"

MAX_BATCH_BYTES = 64 * 1024
"""Upper bound on one batch's stdin payload. The container transport stages
stdin as a single ``printf`` argument, and Linux caps one argument at 128 KiB
(``MAX_ARG_STRLEN``); half that leaves room for shell quoting."""

_SENTINEL_BYTES = len(f'"{SENTINEL_PREFIX}{"0" * 16}"\n')

_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


def new_sentinel() -> str:
    """A fresh sentinel: plain ASCII, so it needs no escaping as a string literal."""
    return SENTINEL_PREFIX + secrets.token_hex(8)


def build_batch_payload(expressions: Sequence[str], sentinel: str) -> str:
    """qna's stdin for ``expressions``: each on one line, each followed by the sentinel."""
    marker = f'"{sentinel}"\n'
    return "".join(normalize_stdin_payload(expression) + marker for expression in expressions)


def _blocks(stdout: str) -> list[str]:
    """qna's answer blocks, with the trailing empty prompt dropped.

    A block ends at a blank line or where the next ``Q:`` prompt starts:
    Linux and Windows qna print both, but the macOS agent's QnA prints no
    prompts at all, so a blank line is all that separates its answers.
    """
    blocks: list[list[str]] = [[]]
    for line in _LINE_SPLIT.split(stdout):
        if not line.strip() or line.startswith("Q:"):
            if blocks[-1]:
                blocks.append([])
            if not line.strip():
                continue
        blocks[-1].append(line)
    texts = ["\n".join(lines) for lines in blocks if lines]
    if texts and not texts[-1].removeprefix("Q:").strip():
        texts.pop()
    return texts


def split_batch_output(stdout: str, count: int, sentinel: str) -> list[str] | None:
    """The ``count`` expression blocks, or ``None`` when the output cannot be trusted."""
    blocks = _blocks(stdout)
    if len(blocks) != 2 * count:
        return None
    for marker in blocks[1::2]:
        parsed = parse_qna_output(marker)
        if parsed.answers != [sentinel] or parsed.errors:
            return None
    return blocks[0::2]


def parse_batch_output(
    stdout: str,
    expressions: Sequence[str],
    sentinel: str,
    **fields: object,
) -> list[ClientRelevanceResult] | None:
    """One result per expression from a batch run's stdout, or ``None`` to fall back.

    ``fields`` are what every result shares -- host, transport, qna_path,
    qna_version, exit_code, elapsed_ms. Each block is classified exactly as a
    single run's output is (with exit 0 and no stderr, since those describe
    the whole process, which the caller has already checked), so an ``E:``
    line stays with its own expression.
    """
    blocks = split_batch_output(stdout, len(expressions), sentinel)
    if blocks is None:
        return None
    results: list[ClientRelevanceResult] = []
    for expression, block in zip(expressions, blocks, strict=True):
        parsed = parse_qna_output(block)
        if not parsed.has_recognizable_output:
            return None
        error, error_kind = classify_qna_outcome(parsed, 0, "")
        results.append(
            ClientRelevanceResult(
                client_relevance=expression,
                answers=parsed.answers,
                answer_types=parsed.answer_types,
                error=error,
                error_kind=error_kind,
                raw_qna_output=block,
                qna_time=parsed.qna_time,
                qna_batch_size=len(expressions),
                **fields,  # type: ignore[arg-type]
            )
        )
    return results


def chunk_expressions(
    expressions: Sequence[str], *, max_count: int, max_bytes: int = MAX_BATCH_BYTES
) -> list[list[str]]:
    """Split ``expressions`` into batches of at most ``max_count`` and ``max_bytes``.

    An expression too big for ``max_bytes`` on its own gets a chunk of its
    own, which the caller evaluates alone anyway.
    """
    if max_count < 1:
        raise ValueError(f"max_count must be at least 1, got {max_count}")
    chunks: list[list[str]] = []
    size = 0
    for expression in expressions:
        cost = len(normalize_stdin_payload(expression).encode("utf-8")) + _SENTINEL_BYTES
        if not chunks or len(chunks[-1]) >= max_count or size + cost > max_bytes:
            chunks.append([])
            size = 0
        chunks[-1].append(expression)
        size += cost
    return chunks


__all__ = [
    "MAX_BATCH_BYTES",
    "build_batch_payload",
    "chunk_expressions",
    "new_sentinel",
    "parse_batch_output",
    "split_batch_output",
]
