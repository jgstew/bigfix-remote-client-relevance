"""Tests for the batchability classifier, one per rule.

Needs only the analyzer and pytest (see README.md for why the flags)::

    uv run --no-project --python 3.12 --exclude-newer-package 'bigfix-relevance-analyzer=0 days' \
        --with 'bigfix-relevance-analyzer==1.43.0' --with pytest \
        python -m pytest -c /dev/null tools/batchability

Every expectation here was also measured with qna 11.0.7 (#78); the comment
on each case says what qna did.
"""

from __future__ import annotations

import pytest
from batchability import batchability  # type: ignore[import-not-found]
from bigfix_relevance_analyzer.typecheck import Plurality


def rules(text: str, **kwargs: object) -> set[str]:
    return {reason.rule for reason in batchability(text, **kwargs).reasons}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "true",
        "42",
        "name of operating system",  # world objects every endpoint has
        "version of client",
        'version of client >= "9"',
        "computer name as lowercase",
        'exists file "/nonexistent"',  # False: exists absorbs the outermost step
        'not exists file "/nonexistent"',
        'number of files "/nonexistent"',  # 0
        "exists (1;2) whose (it > 5)",  # False
        "number of (1;2) whose (it > 5)",  # 0
        'size of file "/nonexistent" | 0',  # 0: `|` answers for its left side
        'concatenation of ("a";"b")',  # aggregates that answer "" or 0 when empty
        "sum of (1;2) whose (it > 5)",
        "exists maximum of (1;2) whose (it > 5)",  # False
        # The analyzer's runtime risks are absorbed by the guard here.
        'exists key "HKLM\\SOFTWARE" of native registry',
        "exists wmi",
    ],
)
def test_safe(text):
    verdict = batchability(text)

    assert verdict.kind == "safe", verdict.reasons
    assert verdict.safe
    assert verdict.plurality is Plurality.SINGULAR


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        # Singular expression refers to nonexistent object.
        ('size of file "/nonexistent"', "singular-lookup"),
        ('value of variable "BFRCR_MISSING" of environment', "singular-lookup"),
        ('value of setting "bfrcr_missing" of client', "singular-lookup"),
        ('line 1 of file "/etc/hosts"', "singular-lookup"),
        # exists absorbs only the outermost step; these error beneath it.
        ('exists (size of file "/nonexistent")', "singular-lookup"),
        ('exists files of folder "/nonexistent"', "singular-lookup"),
        ('number of lines of file "/nonexistent"', "singular-lookup"),
        ('exists (folder "/a" | folder "/b")', "singular-lookup"),
        # A property an existing object may lack.
        ('version of file "/x"', "singular-property"),
        # Errors on an empty operand.
        ("maximum of (1;2) whose (it > 5)", "aggregate-of-plural"),
        ("unique value of (1;2) whose (it > 5)", "aggregate-of-plural"),
        # Refused before it runs.
        ('1 + "a"', "type-error"),
        ("name of", "parse-error"),
        # Plural spelling answers empty where the singular errors.
        ('names of files of folder "/nonexistent"', "singular-lookup"),
        # The checker's own runtime risk, outside any guard.
        ("name of wmi", "runtime-risk"),
    ],
)
def test_error_risk(text, rule):
    verdict = batchability(text)

    assert verdict.kind == "error_risk"
    assert rule in rules(text)


def test_an_inspector_the_platform_lacks_is_an_error_risk_even_after_false_and():
    """qna: `false and exists wmi` -> The operator "wmi" is not defined."""
    for text in ("exists wmi", "false and exists wmi"):
        verdict = batchability(text, platform="rhel")
        assert verdict.kind == "error_risk", text
        assert "not-on-platform" in rules(text, platform="rhel")


def test_an_untaken_if_branch_may_use_an_inspector_the_platform_lacks():
    """qna on Linux: `if false then exists wmi else false` -> False."""
    text = 'if windows of operating system then exists wmi "root/cimv2" else false'

    assert batchability(text, platform="rhel").safe


def test_without_a_platform_no_inspector_is_ruled_out():
    assert batchability("exists wmi").safe


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('names of files of folders "/tmp"', "plural-result"),
        ("(1;2;3)", "plural-result"),
        # Singular, but the filter can leave nothing: qna errors or answers empty.
        ("(it) whose (it > 5) of 2", "whose-may-be-empty"),
    ],
)
def test_plural_risk(text, rule):
    assert batchability(text).kind == "plural_risk"
    assert rule in rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "now",  # Sat, 31 Oct 2026 23:25:11 -0400
        '(1, "a")',  # a tuple inside a tuple is wrapped: ( 1, a )
        '"a, b"',
        '"(a)"',
        '"x" & "%2c y"',  # the escape is a comma too
        'if true then "a,b" else "c"',
    ],
)
def test_split_risk(text):
    verdict = batchability(text)

    assert verdict.kind == "split_risk", verdict.reasons
    assert "comma-in-display" in rules(text)


def test_an_unknown_inspector_is_unknown():
    verdict = batchability("bfrcr nonexistent inspector")

    assert verdict.kind == "unknown"
    assert "unknown-reference" in rules("bfrcr nonexistent inspector")


def test_the_worst_kind_wins():
    """Plural and erroring: the error is what decides."""
    assert batchability('names of files of folder "/nonexistent"').kind == "error_risk"


def test_reasons_point_at_the_node():
    text = 'size of file "/nonexistent"'

    (lookup,) = [r for r in batchability(text).reasons if r.rule == "singular-lookup"]

    assert lookup.span is not None
    assert text[lookup.span.start : lookup.span.end] == 'file "/nonexistent"'
    assert lookup.kind == "error_risk"


def test_to_dict():
    payload = batchability('size of file "/nonexistent"').to_dict()

    assert payload["kind"] == "error_risk"
    assert payload["plurality"] == "singular"
    assert {"rule", "kind", "span", "detail"} <= set(payload["reasons"][0])
