"""Human-readable rendering, available without importing the CLI.

An MCP server wants the same text the CLI prints for its ``content`` block.
Keeping it here rather than in ``cli.py`` means getting it does not drag in
``typer``.
"""

from __future__ import annotations

import ast
import inspect

from bigfix_remote_client_relevance import render
from bigfix_remote_client_relevance.render import format_result, format_results
from bigfix_remote_client_relevance.results import ClientRelevanceResult


def make_result(**overrides) -> ClientRelevanceResult:
    defaults = {"host": "local", "transport": "local", "client_relevance": "now", "answers": ["42"]}
    defaults.update(overrides)
    return ClientRelevanceResult(**defaults)


def test_render_module_does_not_depend_on_the_cli_stack():
    """The whole point of the move: no typer, no click, no cli import.

    Checked against the parsed imports rather than the raw text, so the module
    docstring stays free to explain *why* typer is absent.
    """
    tree = ast.parse(inspect.getsource(render))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(name.split(".")[0] in {"typer", "click"} for name in imported)
    assert "bigfix_remote_client_relevance.cli" not in imported


# --- single results ---------------------------------------------------------


def test_unlabelled_result_is_just_its_answers():
    assert format_result(make_result(answers=["a", "b"])) == "a\nb"


def test_labelled_result_gets_a_header():
    assert format_result(make_result(), labelled=True) == "== local\n42"


def test_error_is_rendered_after_the_answers():
    text = format_result(make_result(answers=[], error="no such property", error_kind="relevance"))

    assert text == "!! relevance: no such property"


def test_ssh_host_is_qualified_by_its_transport():
    text = format_result(make_result(host="192.168.4.115", transport="ssh"), labelled=True)

    assert text.startswith("== ssh:192.168.4.115")


def test_ssh_label_carries_the_platform_and_version():
    text = format_result(
        make_result(host="win11", transport="ssh", platform="windows", qna_version="11.0.6.137"),
        labelled=True,
    )

    assert text.startswith("== ssh:win11:windows (qna 11.0.6.137)")


def test_local_label_omits_the_platform():
    """local and container already say what they are; repeating it is noise."""
    text = format_result(make_result(platform="macos"), labelled=True)

    assert text.startswith("== local\n")


def test_container_label_keeps_its_own_host_shape():
    text = format_result(
        make_result(host="container:ubuntu:22.04@x86_64", transport="container", platform="ubuntu"),
        labelled=True,
    )

    assert text.startswith("== container:ubuntu:22.04@x86_64\n")


def test_an_already_qualified_ssh_host_is_not_double_prefixed():
    text = format_result(make_result(host="ssh:box", transport="ssh"), labelled=True)

    assert text.startswith("== ssh:box\n")


# --- fan-outs ---------------------------------------------------------------


def test_a_single_result_is_not_labelled():
    assert format_results([make_result()]) == "42"


def test_a_fanout_labels_every_section():
    text = format_results(
        [make_result(host="a", transport="ssh"), make_result(host="b", transport="ssh")]
    )

    assert text == "== ssh:a\n42\n== ssh:b\n42"


def test_no_results_renders_empty():
    assert format_results([]) == ""
