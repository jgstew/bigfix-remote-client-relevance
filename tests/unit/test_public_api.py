"""The package's public surface, pinned.

Downstream MCP servers import from the package root and nowhere else, so this
module treats ``__all__`` as the contract: every name in it must import, and no
name a consumer needs may quietly go missing from a submodule's ``__all__``
(the gap that left ``evaluate_client_relevance_stream`` unexported from
``orchestrate`` for a release).
"""

from __future__ import annotations

import importlib

import pytest

import bigfix_remote_client_relevance as pkg

# The intended root surface. Adding to this list is the deliberate act of
# widening the public contract; it is not a mirror of whatever __init__ happens
# to re-export today.
EXPECTED_ROOT_EXPORTS = [
    "BigFixRelevanceError",
    "ClientRelevanceResult",
    "ERROR_KINDS",
    "ERROR_KIND_BOOTSTRAP",
    "ERROR_KIND_QNA",
    "ERROR_KIND_RELEVANCE",
    "ERROR_KIND_RESOLVE",
    "ERROR_KIND_TRANSPORT",
    "EXIT_OK",
    "EXIT_QNA",
    "EXIT_RELEVANCE",
    "EXIT_RESOLVE",
    "EXIT_TRANSPORT",
    "InventoryError",
    "ParsedQnaOutput",
    "RESULT_JSON_SCHEMA",
    "ResolvedQna",
    "ResultPayload",
    "SCHEMA_VERSION",
    "Target",
    "Transport",
    "TransportContainer",
    "TransportFastQuery",
    "TransportLocal",
    "TransportOnlineEvaluator",
    "TransportSSH",
    "count_work",
    "evaluate_client_relevance",
    "evaluate_client_relevance_stream",
    "evaluate_many",
    "evaluate_many_stream",
    "find_qna_path",
    "format_result",
    "format_results",
    "load_inventory",
    "parse_qna_output",
    "reclaim_stray_containers",
    "result_to_dict",
    "results_to_dicts",
    "worst_exit_code",
]


@pytest.mark.parametrize("name", EXPECTED_ROOT_EXPORTS)
def test_root_export_is_importable(name):
    assert name in pkg.__all__, f"{name} missing from bigfix_remote_client_relevance.__all__"
    assert getattr(pkg, name, None) is not None


def test_root_all_has_no_extras():
    """__all__ and the expected surface agree in both directions."""
    assert sorted(pkg.__all__) == sorted(EXPECTED_ROOT_EXPORTS)


# __all__ ordering is not asserted here: ruff's RUF022 already enforces it, and
# duplicating that with a plain sorted() only disagrees with it over case.


def test_version_is_reported():
    """MCP servers report a server version; they must not hardcode ours."""
    assert isinstance(pkg.__version__, str)
    assert pkg.__version__


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (
            "bigfix_remote_client_relevance.orchestrate",
            # The progress pair an MCP server needs: stream for incremental
            # notifications, count_work for the denominator.
            ["count_work", "evaluate_client_relevance_stream"],
        ),
        (
            "bigfix_remote_client_relevance.results",
            [
                "ERROR_KINDS",
                "ERROR_KIND_BOOTSTRAP",
                "ERROR_KIND_QNA",
                "ERROR_KIND_RELEVANCE",
                "ERROR_KIND_RESOLVE",
                "ERROR_KIND_TRANSPORT",
            ],
        ),
    ],
)
def test_submodule_all_covers_public_names(module, names):
    mod = importlib.import_module(module)
    for name in names:
        assert name in mod.__all__, f"{name} missing from {module}.__all__"


def test_error_kinds_matches_the_constants():
    assert set(pkg.ERROR_KINDS) == {
        pkg.ERROR_KIND_BOOTSTRAP,
        pkg.ERROR_KIND_QNA,
        pkg.ERROR_KIND_RELEVANCE,
        pkg.ERROR_KIND_RESOLVE,
        pkg.ERROR_KIND_TRANSPORT,
    }
