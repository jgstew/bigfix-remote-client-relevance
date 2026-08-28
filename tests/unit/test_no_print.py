"""Enforces the project-wide rule: the library logs, it never prints.

stdout is reserved for the CLI's result payload and, later, the stdio MCP
server's JSON-RPC stream. A stray print in the library corrupts that channel,
so the rule is tested rather than merely documented.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

import bigfix_remote_client_relevance
from bigfix_remote_client_relevance.transports.local import TransportLocal

SRC_ROOT = Path(bigfix_remote_client_relevance.__file__).parent

# cli.py is the single sanctioned stdout writer.
STDOUT_EXEMPT = {"cli.py"}

_PRINT_CALL = re.compile(r"(?<![\w.])print\s*\(")


def _library_modules() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if p.name not in STDOUT_EXEMPT)


def test_there_are_modules_to_check():
    assert _library_modules(), "no library modules found - the scan would vacuously pass"


@pytest.mark.parametrize("module", _library_modules(), ids=lambda p: p.name)
def test_module_contains_no_print_call(module):
    source = module.read_text(encoding="utf-8")

    assert not _PRINT_CALL.search(source), f"{module.name} calls print(); use logging instead"


def test_package_root_logger_has_null_handler():
    logger = logging.getLogger("bigfix_remote_client_relevance")

    assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)


def test_library_does_not_configure_logging():
    """A library must leave handlers, levels, and formats to the application."""
    logger = logging.getLogger("bigfix_remote_client_relevance")

    assert logger.level == logging.NOTSET
    assert all(isinstance(h, logging.NullHandler) for h in logger.handlers)


async def test_evaluate_writes_nothing_to_stdout(fake_qna, qna_output, capsys, allow_non_root_macos):
    stub = fake_qna(stdout=qna_output("multi_answer"))

    await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    captured = capsys.readouterr()
    assert captured.out == ""


async def test_failing_evaluate_writes_nothing_to_stdout(fake_qna, capsys, allow_non_root_macos):
    stub = fake_qna(stdout="", stderr="boom\n", exit_code=3)

    await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert capsys.readouterr().out == ""


async def test_debug_log_records_the_command_line(fake_qna, caplog, allow_non_root_macos):
    stub = fake_qna(stdout="A: yes\nT: 0.1 ms\n")

    with caplog.at_level(logging.DEBUG, logger="bigfix_remote_client_relevance"):
        await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "-showtypes" in logged
