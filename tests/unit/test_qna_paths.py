"""Tests for locating a qna binary on the current machine."""

from __future__ import annotations

import shutil

from bigfix_remote_client_relevance import qna_paths
from bigfix_remote_client_relevance.qna_paths import default_candidates, find_qna_path


def test_macos_candidates_include_besagent_app():
    candidates = default_candidates("darwin")

    assert "/Library/BESAgent/BESAgent.app/Contents/MacOS/QnA" in candidates
    assert "/usr/local/bin/qna" in candidates


def test_linux_candidates_include_besclient_bin():
    candidates = default_candidates("linux")

    assert "/opt/BESClient/bin/qna" in candidates


def test_windows_candidates_include_program_files():
    candidates = default_candidates("win32")

    assert any("BES Client" in c and c.lower().endswith("qna.exe") for c in candidates)


def test_find_returns_first_existing_executable(tmp_path):
    missing = tmp_path / "missing"
    present = tmp_path / "qna"
    present.write_text("#!/bin/sh\n")
    present.chmod(0o755)
    later = tmp_path / "later"
    later.write_text("#!/bin/sh\n")
    later.chmod(0o755)

    found = find_qna_path(candidates=[str(missing), str(present), str(later)])

    assert found == str(present)


def test_find_skips_non_executable_files(tmp_path):
    not_executable = tmp_path / "qna"
    not_executable.write_text("data")
    not_executable.chmod(0o644)

    assert find_qna_path(candidates=[str(not_executable)]) is None


def test_find_falls_back_to_path_lookup(monkeypatch, tmp_path):
    on_path = tmp_path / "qna"
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(on_path) if name == "qna" else None)

    assert find_qna_path(candidates=[]) == str(on_path)


def test_find_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert find_qna_path(candidates=[]) is None


def test_find_uses_platform_defaults_when_candidates_omitted(monkeypatch):
    """With no explicit candidates the platform list is consulted."""
    consulted: list[str] = []

    def _fake_default(platform=None):
        consulted.append(platform or "current")
        return ()

    monkeypatch.setattr(qna_paths, "default_candidates", _fake_default)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert find_qna_path() is None
    assert consulted
