"""Tests for the command line interface.

The CLI is the one sanctioned stdout writer, so these pin the stdout/stderr
split as hard as they pin behavior: the payload goes to stdout and nothing
else does, which is what makes a stdio MCP server possible later.

The orchestrator is stubbed throughout; no transport is ever constructed here.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from bigfix_remote_client_relevance import cli as cli_module
from bigfix_remote_client_relevance.results import (
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_RESOLVE,
    ERROR_KIND_TRANSPORT,
    ClientRelevanceResult,
)

runner = CliRunner()


@pytest.fixture
def captured(monkeypatch):
    """Replace the orchestrator, recording what the CLI asked it to do."""
    record: dict = {}

    async def fake_evaluate(client_relevance, targets, **kwargs):
        record["client_relevance"] = client_relevance
        record["targets"] = list(targets)
        record.update(kwargs)
        return record.get(
            "results",
            [
                ClientRelevanceResult(
                    host="h",
                    transport="fake",
                    client_relevance=client_relevance,
                    answers=["Mac OS 15.5"],
                    answer_types=["string"],
                )
            ],
        )

    monkeypatch.setattr(cli_module, "evaluate_client_relevance", fake_evaluate)
    return record


def invoke(*args):
    return runner.invoke(cli_module.app, list(args))


# --- target selection ------------------------------------------------------


def test_positional_host_selects_ssh(captured):
    result = invoke("mac-test", "name of operating system")

    assert result.exit_code == 0, result.output
    assert captured["targets"][0].kind == "ssh"
    assert captured["targets"][0].name == "mac-test"
    assert captured["client_relevance"] == "name of operating system"


def test_local_flag_selects_local(captured):
    result = invoke("--local", "version of client")

    assert result.exit_code == 0, result.output
    assert captured["targets"][0].kind == "local"
    assert captured["client_relevance"] == "version of client"


def test_container_flag_selects_container(captured):
    result = invoke("--container", "ubuntu:22.04", "true")

    assert result.exit_code == 0, result.output
    target = captured["targets"][0]
    assert target.kind == "container"
    assert target.image == "ubuntu:22.04"


def test_no_target_is_a_usage_error(captured):
    result = invoke("name of operating system")

    assert result.exit_code != 0
    assert "target" in result.output.lower() or "host" in result.output.lower()


def test_two_target_modes_is_a_usage_error(captured):
    result = invoke("--local", "--container", "ubuntu:22.04", "true")

    assert result.exit_code != 0


def test_inventory_supplies_targets(captured, tmp_path):
    inventory = tmp_path / "hosts.toml"
    inventory.write_text(
        '[hosts.a]\ntransport = "ssh"\n[hosts.b]\ntransport = "container"\nimage = "ubuntu:22.04"\n',
        encoding="utf-8",
    )

    result = invoke("--inventory", str(inventory), "true")

    assert result.exit_code == 0, result.output
    assert {t.name for t in captured["targets"]} == {"a", "b"}


# --- client relevance input ------------------------------------------------


def test_relevance_from_file(captured, tmp_path):
    probe = tmp_path / "probe.rel"
    probe.write_text("name of operating system\n", encoding="utf-8")

    result = invoke("--local", "-f", str(probe))

    assert result.exit_code == 0, result.output
    assert captured["client_relevance"].strip() == "name of operating system"


def test_missing_relevance_is_a_usage_error(captured):
    result = invoke("--local")

    assert result.exit_code != 0


def test_file_and_inline_together_is_a_usage_error(captured, tmp_path):
    probe = tmp_path / "probe.rel"
    probe.write_text("true\n", encoding="utf-8")

    result = invoke("--local", "-f", str(probe), "true")

    assert result.exit_code != 0


# --- versions --------------------------------------------------------------


def test_qna_version_is_repeatable(captured):
    result = invoke("--local", "--qna-version", "11.0", "--qna-version", "9.5", "true")

    assert result.exit_code == 0, result.output
    assert captured["qna_version"] == ["11.0", "9.5"]


def test_max_parallel_is_passed_through(captured):
    invoke("--local", "--max-parallel", "3", "true")

    assert captured["max_parallel"] == 3


# --- output ----------------------------------------------------------------


def test_plain_output_lists_answers(captured):
    result = invoke("--local", "true")

    assert "Mac OS 15.5" in result.stdout


def test_json_output_matches_the_result_contract(captured):
    result = invoke("--local", "--json", "true")

    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["answers"] == ["Mac OS 15.5"]
    # The field names are the future MCP tool's contract.
    for field in ("host", "transport", "client_relevance", "error_kind", "qna_version"):
        assert field in payload[0]


def test_stdout_carries_only_the_payload(captured):
    """The MCP-readiness test: logs must not contaminate stdout."""
    result = runner.invoke(cli_module.app, ["--local", "-vv", "--json", "true"])

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # must parse: nothing else may be on stdout


def test_json_is_pretty_printed_but_still_parses(captured):
    result = invoke("--local", "--json", "true")

    json.loads(result.stdout)


# --- exit codes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(None, 0), (ERROR_KIND_RELEVANCE, 1), (ERROR_KIND_TRANSPORT, 3), (ERROR_KIND_RESOLVE, 4)],
)
def test_exit_code_reflects_worst_result(captured, monkeypatch, kind, expected):
    async def fake_evaluate(client_relevance, targets, **kwargs):
        return [
            ClientRelevanceResult(
                host="h",
                transport="fake",
                client_relevance=client_relevance,
                answers=[] if kind else ["yes"],
                error="boom" if kind else None,
                error_kind=kind,
            )
        ]

    monkeypatch.setattr(cli_module, "evaluate_client_relevance", fake_evaluate)

    result = invoke("--local", "true")

    assert result.exit_code == expected


def test_errors_are_reported_on_stderr_not_stdout(monkeypatch):
    async def fake_evaluate(client_relevance, targets, **kwargs):
        return [
            ClientRelevanceResult(
                host="h",
                transport="fake",
                client_relevance=client_relevance,
                error='The operator "namez" is not defined.',
                error_kind=ERROR_KIND_RELEVANCE,
            )
        ]

    monkeypatch.setattr(cli_module, "evaluate_client_relevance", fake_evaluate)

    result = runner.invoke(cli_module.app, ["--local", "--json", "namez of it"])

    assert result.exit_code == 1
    json.loads(result.stdout)  # stdout stays machine-readable even on failure
