"""Tests for TransportLocal.

These spawn real stub executables rather than mocking subprocess: the bugs
worth catching here (stdin encoding, prefix stripping, timeout kill, exit-code
mapping) only appear when a process is actually run.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports.local import TransportLocal

pytestmark = pytest.mark.usefixtures("allow_non_root_macos")


async def test_evaluate_success(fake_qna, qna_output):
    stub = fake_qna(stdout=qna_output("single_answer"))

    result = await TransportLocal().evaluate_client_relevance(
        "name of operating system", qna_path=stub.path
    )

    assert result.transport == "local"
    assert result.host == "local"
    assert result.client_relevance == "name of operating system"
    assert result.answers == ["Mac OS 15.5"]
    assert result.answer_types == ["singular string"]
    assert result.qna_time == "0.163 ms"
    assert result.error is None
    assert result.error_kind is None
    assert result.exit_code == 0
    assert result.qna_path == stub.path
    assert result.elapsed_ms >= 0
    assert result.raw_qna_output == qna_output("single_answer")


async def test_qna_invoked_with_t_and_showtypes(fake_qna):
    stub = fake_qna(stdout="A: yes\n")

    await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert stub.argv == ["-t", "-showtypes"]


async def test_leading_q_prefix_stripped_from_stdin(fake_qna):
    """stdin mode has no `Q:` vocabulary; the prefix must be removed."""
    stub = fake_qna(stdout="A: yes\n")

    await TransportLocal().evaluate_client_relevance(
        "Q: name of operating system", qna_path=stub.path
    )

    assert stub.stdin_text == "name of operating system\n"


async def test_trailing_newline_appended(fake_qna):
    stub = fake_qna(stdout="A: yes\n")

    await TransportLocal().evaluate_client_relevance("version of client", qna_path=stub.path)

    assert stub.stdin_text.endswith("\n")
    assert stub.stdin_text == "version of client\n"


async def test_existing_trailing_newline_not_doubled(fake_qna):
    stub = fake_qna(stdout="A: yes\n")

    await TransportLocal().evaluate_client_relevance("version of client\n", qna_path=stub.path)

    assert stub.stdin_text == "version of client\n"


async def test_stdin_is_utf8_encoded(fake_qna):
    stub = fake_qna(stdout="A: ok\n")

    await TransportLocal().evaluate_client_relevance('"Zürich"', qna_path=stub.path)

    assert stub.stdin_bytes == '"Zürich"\n'.encode()


async def test_relevance_error_maps_to_relevance_kind(fake_qna, qna_output):
    stub = fake_qna(stdout=qna_output("relevance_error"))

    result = await TransportLocal().evaluate_client_relevance("namez of it", qna_path=stub.path)

    assert result.error_kind == ERROR_KIND_RELEVANCE
    assert result.error == 'The operator "namez" is not defined.'
    assert result.answers == []


async def test_nonzero_exit_maps_to_qna_kind(fake_qna):
    stub = fake_qna(stdout="", stderr="qna: fatal internal error\n", exit_code=2)

    result = await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert result.error_kind == ERROR_KIND_QNA
    assert result.exit_code == 2
    assert "fatal internal error" in (result.error or "")


async def test_unparseable_output_maps_to_qna_kind(fake_qna):
    """Exit 0 but nothing recognizable on any channel is a qna failure."""
    stub = fake_qna(stdout="totally unexpected output\n")

    result = await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert result.error_kind == ERROR_KIND_QNA


async def test_empty_but_valid_result_is_success(fake_qna, qna_output):
    """Zero answers with a valid T:/I: transcript is not an error."""
    stub = fake_qna(stdout=qna_output("empty_result"))

    result = await TransportLocal().evaluate_client_relevance("nothing", qna_path=stub.path)

    assert result.error_kind is None
    assert result.answers == []


async def test_timeout_maps_to_transport_and_kills_process(fake_qna):
    stub = fake_qna(stdout="A: too late\n", sleep=5.0)

    result = await TransportLocal().evaluate_client_relevance(
        "true", qna_path=stub.path, timeout_s=0.3
    )

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert "timed out" in (result.error or "").lower()
    assert not stub.ran_to_completion, "process outlived the timeout"


async def test_missing_binary_maps_to_bootstrap(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = await TransportLocal(candidates=[]).evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "qna" in (result.error or "").lower()


async def test_invalid_utf8_output_is_replaced_not_raised(fake_qna):
    stub = fake_qna(stdout_bytes=b"A: caf\xff\xfe\nI: singular string\nT: 0.1 ms\n")

    result = await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert result.error_kind is None
    assert result.answers and result.answers[0].startswith("caf")


async def test_constructor_qna_path_used_as_default(fake_qna):
    stub = fake_qna(stdout="A: yes\nT: 0.1 ms\n")

    result = await TransportLocal(qna_path=stub.path).evaluate_client_relevance("true")

    assert result.qna_path == stub.path
    assert result.answers == ["yes"]


async def test_explicit_qna_path_overrides_constructor(fake_qna):
    default_stub = fake_qna(stdout="A: default\nT: 0.1 ms\n")
    override_stub = fake_qna(stdout="A: override\nT: 0.1 ms\n")

    result = await TransportLocal(qna_path=default_stub.path).evaluate_client_relevance(
        "true", qna_path=override_stub.path
    )

    assert result.answers == ["override"]


async def test_resolved_qna_not_supported_until_m3(fake_qna, tmp_path):
    """TransportLocal accepts the kwarg for protocol compliance; M3 implements it."""
    stub = fake_qna(stdout="A: yes\n")
    artifact = tmp_path / "QNA11.0.6.137.zip"
    artifact.touch()

    result = await TransportLocal().evaluate_client_relevance(
        "true",
        qna_path=stub.path,
        qna=ResolvedQna(version="11.0.6.137", artifact_path=artifact),
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX-only")
async def test_macos_non_root_fails_fast_with_bootstrap_error(fake_qna, monkeypatch):
    """Refuse before spawning: non-root qna on macOS aborts with a C++ crash.

    Observed against BESAgent 11.x on macOS 15 — even `TRUE` dies with an
    uncaught FileIOError rather than answering or reporting an E: line, so a
    clear pre-flight error beats surfacing that crash dump to the user.
    """
    stub = fake_qna(stdout="A: unreachable\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)

    result = await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "root" in (result.error or "").lower()
    assert "sudo" in (result.error or "").lower()
    assert not stub.was_invoked, "qna should not be spawned when it cannot work"


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX-only")
async def test_macos_root_check_can_be_waived(fake_qna, monkeypatch, caplog):
    """Escape hatch for setups where non-root qna does work."""
    stub = fake_qna(stdout="A: yes\nT: 0.1 ms\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)

    with caplog.at_level(logging.WARNING):
        result = await TransportLocal(require_root_on_macos=False).evaluate_client_relevance(
            "true", qna_path=stub.path
        )

    assert result.error_kind is None
    assert result.answers == ["yes"]
    assert any("root" in record.message.lower() for record in caplog.records)


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX-only")
async def test_macos_as_root_proceeds_without_warning(fake_qna, monkeypatch, caplog):
    stub = fake_qna(stdout="A: yes\nT: 0.1 ms\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    with caplog.at_level(logging.WARNING):
        result = await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert result.error_kind is None
    assert not [r for r in caplog.records if "root" in r.message.lower()]


@pytest.mark.skipif(sys.platform == "win32", reason="geteuid is POSIX-only")
async def test_root_check_does_not_apply_off_macos(fake_qna, monkeypatch):
    stub = fake_qna(stdout="A: yes\nT: 0.1 ms\n")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)

    result = await TransportLocal().evaluate_client_relevance("true", qna_path=stub.path)

    assert result.error_kind is None
