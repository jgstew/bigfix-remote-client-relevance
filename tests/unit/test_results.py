"""Tests for qna output parsing and the ClientRelevanceResult contract."""

from __future__ import annotations

import dataclasses

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_RESOLVE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
    ResolvedQna,
    parse_qna_output,
)


def test_parse_single_answer(qna_output):
    parsed = parse_qna_output(qna_output("single_answer"))

    assert parsed.answers == ["Mac OS 15.5"]
    assert parsed.answer_types == ["singular string"]
    assert parsed.errors == []
    assert parsed.qna_time == "0.163 ms"


def test_parse_multi_answer_preserves_order(qna_output):
    parsed = parse_qna_output(qna_output("multi_answer"))

    assert parsed.answers == ["alpha", "beta", "gamma"]
    assert parsed.answer_types == ["plural string"]


def test_parse_relevance_error(qna_output):
    parsed = parse_qna_output(qna_output("relevance_error"))

    assert parsed.answers == []
    assert parsed.errors == ['The operator "namez" is not defined.']


def test_parse_q_a_echo_variant(qna_output):
    """Interactive echo puts the answer on the prompt line: `Q: A: <answer>`."""
    parsed = parse_qna_output(qna_output("echo_q_variant"))

    assert parsed.answers == ["11.0.4.60"]
    assert parsed.answer_types == ["singular version"]


def test_parse_without_showtypes_leaves_types_empty(qna_output):
    parsed = parse_qna_output(qna_output("no_showtypes"))

    assert parsed.answers == ["Ubuntu", "22.04"]
    assert parsed.answer_types == []
    assert parsed.qna_time == "0.900 ms"


def test_parse_empty_result_is_not_an_error(qna_output):
    """A relevance that legitimately answers nothing is a success, not a failure."""
    parsed = parse_qna_output(qna_output("empty_result"))

    assert parsed.answers == []
    assert parsed.errors == []
    assert parsed.answer_types == ["plural string"]


def test_parse_mixed_answer_and_error(qna_output):
    parsed = parse_qna_output(qna_output("mixed_answer_error"))

    assert parsed.answers == ["Mac OS 15.5"]
    assert parsed.errors == ['The operator "bogus" is not defined.']


def test_channel_prefixes_are_anchored_at_line_start(qna_output):
    """A prefix appearing mid-line is content, not a channel marker."""
    parsed = parse_qna_output(qna_output("midline_prefix_trap"))

    # Only the first "A: " is stripped; the rest of the line is the answer.
    assert parsed.answers == ["A: fake and E: also fake"]
    assert parsed.errors == []


def test_parse_handles_crlf_line_endings(qna_output):
    parsed = parse_qna_output(qna_output("crlf_windows"))

    assert parsed.answers == ["11.0.6.137"]
    assert parsed.answer_types == ["singular version"]
    assert parsed.qna_time == "0.075 ms"


def test_parse_preserves_non_ascii(qna_output):
    parsed = parse_qna_output(qna_output("utf8_answer"))

    assert parsed.answers == ["Zürich"]


def test_i_lines_are_types_and_t_lines_are_time(qna_output):
    """The I: channel carries result types; T: carries elapsed time.

    DESIGN.md originally attributed types to T:; the qna output format and the
    run_qna.yaml step-summary sections both show otherwise.
    """
    parsed = parse_qna_output(qna_output("single_answer"))

    assert parsed.answer_types == ["singular string"]
    assert "ms" in (parsed.qna_time or "")
    assert parsed.qna_time not in parsed.answer_types


def test_parse_empty_string():
    parsed = parse_qna_output("")

    assert parsed.answers == []
    assert parsed.errors == []
    assert parsed.answer_types == []
    assert parsed.qna_time is None


def test_result_has_every_designed_field():
    """Locks the JSON / future-MCP contract."""
    names = {f.name for f in dataclasses.fields(ClientRelevanceResult)}

    assert names == {
        "host",
        "transport",
        "client_relevance",
        "answers",
        "answer_types",
        "error",
        "error_kind",
        "raw_qna_output",
        "qna_path",
        "qna_version",
        "qna_time",
        "elapsed_ms",
        "exit_code",
    }


def test_result_defaults_are_not_shared_between_instances():
    first = ClientRelevanceResult(host="a", transport="local", client_relevance="true")
    second = ClientRelevanceResult(host="b", transport="local", client_relevance="true")
    first.answers.append("x")

    assert second.answers == []


def test_error_kind_constants_match_design():
    assert ERROR_KIND_RELEVANCE == "relevance"
    assert ERROR_KIND_QNA == "qna"
    assert ERROR_KIND_BOOTSTRAP == "bootstrap"
    assert ERROR_KIND_TRANSPORT == "transport"
    assert ERROR_KIND_RESOLVE == "resolve"


def test_resolved_qna_carries_full_version_and_artifact(tmp_path):
    artifact = tmp_path / "QNA11.0.6.137.zip"
    artifact.touch()
    resolved = ResolvedQna(version="11.0.6.137", artifact_path=artifact)

    assert resolved.version == "11.0.6.137"
    assert resolved.artifact_path == artifact


@pytest.mark.parametrize("spec", ["11.0", "10.0", ""])
def test_resolved_qna_rejects_version_specs(spec):
    """ResolvedQna must hold a resolved 4-part version, never a spec."""
    with pytest.raises(ValueError):
        ResolvedQna(version=spec, artifact_path=None)
