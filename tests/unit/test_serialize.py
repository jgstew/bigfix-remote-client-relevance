"""The JSON wire shape and the schema that describes it.

This is the contract both the CLI's ``--json``/``--jsonl`` output and any MCP
server's ``structuredContent`` are built from, so the tests here are mostly
about the shape staying honest rather than about behaviour.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from bigfix_remote_client_relevance.results import ClientRelevanceResult
from bigfix_remote_client_relevance.serialize import (
    RESULT_JSON_SCHEMA,
    SCHEMA_VERSION,
    result_to_dict,
    results_to_dicts,
)


def make_result(**overrides) -> ClientRelevanceResult:
    defaults = {
        "host": "local",
        "transport": "local",
        "client_relevance": "now",
        "answers": ["Fri, 28 Aug 2026 12:00:00 +0000"],
        "answer_types": ["singular time"],
        "raw_qna_output": "Q: now\nA: Fri, 28 Aug 2026 12:00:00 +0000\nT: 0.1 ms\n",
        "qna_path": "/usr/local/bin/qna",
        "qna_version": "11.0.6.137",
        "qna_time": "0.1 ms",
        "elapsed_ms": 42,
        "platform": "macos",
    }
    defaults.update(overrides)
    return ClientRelevanceResult(**defaults)


# --- the shape stays in sync ------------------------------------------------


def test_schema_properties_match_the_emitted_keys():
    """The guard that stops the schema rotting when a field is added."""
    emitted = set(result_to_dict(make_result()))
    described = set(RESULT_JSON_SCHEMA["properties"])

    assert emitted == described


def test_emitted_keys_are_the_dataclass_fields_plus_ok():
    emitted = set(result_to_dict(make_result()))
    fields = {f.name for f in dataclasses.fields(ClientRelevanceResult)}

    assert emitted == fields | {"ok"}


def test_schema_requires_every_property():
    assert set(RESULT_JSON_SCHEMA["required"]) == set(RESULT_JSON_SCHEMA["properties"])


def test_schema_is_json_serializable_and_identifies_itself():
    json.dumps(RESULT_JSON_SCHEMA)

    assert RESULT_JSON_SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "$id" in RESULT_JSON_SCHEMA
    assert RESULT_JSON_SCHEMA["x-schema-version"] == SCHEMA_VERSION


def test_key_order_matches_the_declared_contract():
    from bigfix_remote_client_relevance.serialize import _KEY_ORDER

    assert tuple(result_to_dict(make_result())) == _KEY_ORDER


def test_key_order_is_stable():
    """Consumers diffing raw JSONL output should not see gratuitous churn."""
    first = list(result_to_dict(make_result()))
    second = list(result_to_dict(make_result(host="other")))

    assert first == second
    assert first[0] == "host"


# --- the derived ok field ---------------------------------------------------


def test_ok_is_true_for_a_clean_result():
    assert result_to_dict(make_result())["ok"] is True


def test_ok_is_false_when_an_error_kind_is_set():
    payload = result_to_dict(make_result(error="no such property", error_kind="relevance"))

    assert payload["ok"] is False
    assert payload["error_kind"] == "relevance"


# --- raw output capping -----------------------------------------------------


def test_raw_output_is_untouched_by_default():
    result = make_result()

    assert result_to_dict(result)["raw_qna_output"] == result.raw_qna_output


def test_cap_larger_than_the_payload_is_a_no_op():
    result = make_result()
    capped = result_to_dict(result, max_raw_output=10_000)["raw_qna_output"]

    assert capped == result.raw_qna_output


def test_cap_keeps_a_prefix_and_marks_the_truncation():
    result = make_result(raw_qna_output="x" * 500)
    capped = result_to_dict(result, max_raw_output=100)["raw_qna_output"]

    assert capped.startswith("x" * 100)
    assert not capped.startswith("x" * 101)
    assert "truncated" in capped
    assert "500" in capped


def test_cap_of_zero_keeps_nothing():
    capped = result_to_dict(make_result(raw_qna_output="x" * 10), max_raw_output=0)[
        "raw_qna_output"
    ]

    assert "x" not in capped, "a cap of zero keeps no payload at all"
    assert "truncated" in capped


def test_negative_cap_is_rejected():
    with pytest.raises(ValueError, match="max_raw_output"):
        result_to_dict(make_result(), max_raw_output=-1)


def test_capping_does_not_mutate_the_result():
    result = make_result(raw_qna_output="x" * 500)
    result_to_dict(result, max_raw_output=10)

    assert result.raw_qna_output == "x" * 500


# --- lists ------------------------------------------------------------------


def test_results_to_dicts_preserves_order_and_applies_the_cap():
    results = [make_result(host="a", raw_qna_output="y" * 50), make_result(host="b")]
    payloads = results_to_dicts(results, max_raw_output=10)

    assert [p["host"] for p in payloads] == ["a", "b"]
    assert "truncated" in payloads[0]["raw_qna_output"]


def test_results_to_dicts_of_nothing_is_an_empty_list():
    assert results_to_dicts([]) == []


def test_payload_round_trips_through_json():
    payload = result_to_dict(make_result())

    assert json.loads(json.dumps(payload)) == payload
