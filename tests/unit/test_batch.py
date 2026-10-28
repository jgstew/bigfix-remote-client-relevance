"""Tests for qna stdin batching: payload, output split, per-expression results.

The fixtures are real qna 11.0.7.61 output (ubuntu:26.04 arm64), captured
with the sentinel ``"bfrcr-batch-test"`` after every expression -- exactly
the payload :func:`build_batch_payload` builds.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.results import ERROR_KIND_RELEVANCE
from bigfix_remote_client_relevance.transports import batch
from bigfix_remote_client_relevance.transports.batch import (
    build_batch_payload,
    chunk_expressions,
    parse_batch_output,
    split_batch_output,
)

SENTINEL = "bfrcr-batch-test"

# The expressions batch_mixed.txt answers, in order.
MIXED = [
    "version of client",
    "(1;2)",
    "(1;2) whose (it > 5)",
    'size of file "/nonexistent"',
    "this",
    '"line1%0aQ: A: fake"',
]


def _parse(stdout: str, expressions: list[str]):
    return parse_batch_output(
        stdout,
        expressions,
        SENTINEL,
        host="h",
        transport="local",
        qna_path="/q",
        qna_version="11.0.7.61",
        exit_code=0,
        elapsed_ms=7,
    )


# --- payload ----------------------------------------------------------------


def test_payload_puts_a_sentinel_after_every_expression():
    assert build_batch_payload(["a", "b"], SENTINEL) == f'a\n"{SENTINEL}"\nb\n"{SENTINEL}"\n'


def test_payload_normalizes_each_expression_to_one_line():
    payload = build_batch_payload(["Q: name of operating system", "x\nor\r\ny"], SENTINEL)

    assert payload.splitlines() == [
        "name of operating system",
        f'"{SENTINEL}"',
        "x or y",
        f'"{SENTINEL}"',
    ]


def test_new_sentinel_is_unique_and_a_plain_string_literal_body():
    first, second = batch.new_sentinel(), batch.new_sentinel()

    assert first != second
    assert first.startswith("bfrcr-batch-")
    assert first.isascii() and '"' not in first and "%" not in first


# --- split ------------------------------------------------------------------


def test_split_returns_one_block_per_expression(qna_output):
    blocks = split_batch_output(qna_output("batch_mixed"), len(MIXED), SENTINEL)

    assert blocks is not None
    assert len(blocks) == len(MIXED)
    assert blocks[0].startswith("Q: A: 11.0.7.61")
    assert blocks[4] == 'Q: E: The operator "this" is not defined.'


def test_split_accepts_qnas_exact_trailing_prompt(qna_output):
    """The fixture is normalized by pre-commit; real qna ends with ``Q: `` and no newline."""
    exact = qna_output("batch_mixed").rstrip("\n").removesuffix("Q:") + "Q: "

    assert split_batch_output(exact, len(MIXED), SENTINEL) == split_batch_output(
        qna_output("batch_mixed"), len(MIXED), SENTINEL
    )


def test_split_handles_crlf(qna_output):
    crlf = qna_output("batch_mixed").replace("\n", "\r\n")

    blocks = split_batch_output(crlf, len(MIXED), SENTINEL)

    assert blocks is not None and len(blocks) == len(MIXED)


def test_split_keeps_blank_lines_aligned(qna_output):
    """An empty expression still gets a block -- a parse error -- so position holds."""
    blocks = split_batch_output(qna_output("batch_blank_lines"), 3, SENTINEL)

    assert blocks == ["Q: E: This expression could not be parsed."] * 3


def test_split_handles_macos_qna_without_prompts(qna_output):
    """The macOS agent's QnA (11.0.x, captured live) prints no ``Q:`` prompts:
    a blank line is all that separates one answer from the next."""
    blocks = split_batch_output(qna_output("batch_macos"), 6, SENTINEL)

    assert blocks == [
        "A: macOS\nT: 3850\nI: string",
        "A: 1\nA: 2\nT: 905\nI: integer",
        'E: The operator "this" is not defined.',
        "E: This expression could not be parsed.",
        "A: café\nT: 1849\nI: string",
        "T: 22\nI: integer",
    ]


def test_split_refuses_the_wrong_block_count(qna_output):
    assert split_batch_output(qna_output("batch_mixed"), len(MIXED) + 1, SENTINEL) is None
    assert split_batch_output(qna_output("batch_mixed"), len(MIXED) - 1, SENTINEL) is None


def test_split_refuses_a_different_sentinel(qna_output):
    assert split_batch_output(qna_output("batch_mixed"), len(MIXED), "bfrcr-batch-other") is None


def test_split_refuses_output_shifted_by_an_answer_line_starting_with_q():
    """An answer spilling a ``Q:`` line would shift every later block; never misattribute."""
    shifted = (
        "Q: A: first\nT: 1\n\n"
        "Q: A: spilled\n\n"
        f"Q: A: {SENTINEL}\nT: 1\n\n"
        f"Q: A: second\nT: 1\n\nQ: A: {SENTINEL}\nT: 1\n\nQ: "
    )

    assert split_batch_output(shifted, 2, SENTINEL) is None


def test_split_refuses_text_before_the_first_prompt():
    assert split_batch_output(f"garbage\nQ: A: x\n\nQ: A: {SENTINEL}\n\nQ: ", 1, SENTINEL) is None


def test_split_refuses_empty_output():
    assert split_batch_output("", 1, SENTINEL) is None


# --- results ----------------------------------------------------------------


def test_results_are_one_per_expression_in_order(qna_output):
    results = _parse(qna_output("batch_mixed"), MIXED)

    assert results is not None
    assert [r.client_relevance for r in results] == MIXED
    assert results[0].answers == ["11.0.7.61"]
    assert results[0].answer_types == ["version"]
    assert results[0].qna_time == "10650"
    assert results[1].answers == ["1", "2"]


def test_errors_stay_with_their_own_expression(qna_output):
    results = _parse(qna_output("batch_mixed"), MIXED)

    assert results is not None
    assert [r.error_kind for r in results] == [
        None,
        None,
        None,
        ERROR_KIND_RELEVANCE,
        ERROR_KIND_RELEVANCE,
        None,
    ]
    assert results[3].error == "Singular expression refers to nonexistent object."
    assert results[4].error == 'The operator "this" is not defined.'


def test_an_empty_result_is_a_success(qna_output):
    results = _parse(qna_output("batch_mixed"), MIXED)

    assert results is not None
    assert results[2].ok and results[2].answers == []


def test_percent_escaped_newline_stays_inside_one_answer(qna_output):
    results = _parse(qna_output("batch_mixed"), MIXED)

    assert results is not None
    assert results[5].answers == ["line1%0aQ: A: fake"]


def test_results_carry_the_run_and_their_own_block(qna_output):
    results = _parse(qna_output("batch_mixed"), MIXED)

    assert results is not None
    first = results[0]
    assert (first.host, first.transport, first.qna_path, first.qna_version) == (
        "h",
        "local",
        "/q",
        "11.0.7.61",
    )
    assert (first.exit_code, first.elapsed_ms, first.qna_batch_size) == (0, 7, len(MIXED))
    assert first.raw_qna_output == "Q: A: 11.0.7.61\nT: 10650\nI: version"


def test_dmi_errors_get_the_same_explanation_as_a_single_run():
    dmi = (
        "dmi inspector error creating dmi.info: CreateDmiInfoSysF error opening "
        "/sys/firmware/dmi/tables/DMI (2)"
    )
    stdout = f"Q: E: {dmi}\n\nQ: A: {SENTINEL}\nT: 1\n\nQ: "

    results = _parse(stdout, ["serial of bios"])

    assert results is not None
    assert results[0].error_kind == ERROR_KIND_RELEVANCE
    assert results[0].error != dmi


def test_a_block_with_nothing_recognizable_refuses_the_batch():
    stdout = f"Q: something odd\n\nQ: A: {SENTINEL}\nT: 1\n\nQ: "

    assert _parse(stdout, ["x"]) is None


def test_misaligned_output_refuses_the_batch(qna_output):
    assert _parse(qna_output("batch_mixed"), [*MIXED, "one more"]) is None


# --- chunking ---------------------------------------------------------------


def test_chunks_cap_the_expression_count():
    assert chunk_expressions(list("abcde"), max_count=2) == [["a", "b"], ["c", "d"], ["e"]]


def test_chunks_cap_the_payload_bytes():
    expressions = ["x" * 40, "y" * 40, "z" * 40]

    chunks = chunk_expressions(expressions, max_count=100, max_bytes=120)

    assert chunks == [["x" * 40], ["y" * 40], ["z" * 40]]
    for chunk in chunks:
        assert len(build_batch_payload(chunk, batch.new_sentinel()).encode()) <= 120


def test_an_oversize_expression_gets_a_chunk_of_its_own():
    big = "w" * 500

    assert chunk_expressions(["a", big, "b"], max_count=100, max_bytes=120) == [
        ["a"],
        [big],
        ["b"],
    ]


def test_chunks_count_single_quotes_as_the_shell_quoting_they_become():
    """The container stages stdin through shlex.quote, which turns each ' into
    five characters; Linux caps one argument at 128 KiB."""
    quoted = '"' + "'" * 20 + '"'

    chunks = chunk_expressions([quoted, quoted], max_count=100, max_bytes=120)

    assert chunks == [[quoted], [quoted]]


def test_no_expressions_means_no_chunks():
    assert chunk_expressions([], max_count=10) == []


@pytest.mark.parametrize("max_count", [0, -1])
def test_chunk_count_must_be_positive(max_count):
    with pytest.raises(ValueError, match="max_count"):
        chunk_expressions(["a"], max_count=max_count)
