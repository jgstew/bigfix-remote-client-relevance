"""Tests for remembering which qna builds are too new for a target.

Without this, every run against an old-runtime target pays for one doomed
attempt at the newest build before falling back.
"""

from __future__ import annotations

import json

from bigfix_remote_client_relevance.bootstrap.compat_memory import CompatMemory

KEY = "container:debian:11:debian@arm64"


def test_nothing_is_remembered_at_first(tmp_path):
    assert CompatMemory(tmp_path / "m.json").lookup(KEY, "11.0.7.61") is None


def test_a_recorded_fallback_redirects_the_same_version(tmp_path):
    memory = CompatMemory(tmp_path / "m.json")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"


def test_a_newer_version_is_redirected_too(tmp_path):
    """Runtime requirements only grow, so newer than too-new is too new."""
    memory = CompatMemory(tmp_path / "m.json")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert memory.lookup(KEY, "11.0.9.10") == "11.0.6.137"


def test_an_older_version_is_left_alone(tmp_path):
    memory = CompatMemory(tmp_path / "m.json")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert memory.lookup(KEY, "11.0.5.204") is None
    assert memory.lookup(KEY, "11.0.6.137") is None


def test_other_targets_are_unaffected(tmp_path):
    memory = CompatMemory(tmp_path / "m.json")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert memory.lookup("container:debian:13:debian@arm64", "11.0.7.61") is None


def test_memory_survives_a_new_instance(tmp_path):
    CompatMemory(tmp_path / "m.json").record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert CompatMemory(tmp_path / "m.json").lookup(KEY, "11.0.7.61") == "11.0.6.137"


def test_the_lowest_too_new_version_is_kept(tmp_path):
    memory = CompatMemory(tmp_path / "m.json")
    memory.record(KEY, too_new="11.0.9.10", works="11.0.6.137")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert memory.lookup(KEY, "11.0.8.1") == "11.0.6.137"


def test_entries_expire_so_an_upgraded_image_is_rechecked(tmp_path):
    now = [1_000_000.0]
    memory = CompatMemory(tmp_path / "m.json", ttl_s=60, clock=lambda: now[0])
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    now[0] += 61

    assert memory.lookup(KEY, "11.0.7.61") is None


def test_forget_drops_an_entry(tmp_path):
    memory = CompatMemory(tmp_path / "m.json")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    memory.forget(KEY)

    assert CompatMemory(tmp_path / "m.json").lookup(KEY, "11.0.7.61") is None


def test_a_corrupt_file_reads_as_empty(tmp_path):
    path = tmp_path / "m.json"
    path.write_text("{not json", encoding="utf-8")

    assert CompatMemory(path).lookup(KEY, "11.0.7.61") is None


def test_a_malformed_entry_reads_as_absent(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({KEY: {"too_new": "nope"}}), encoding="utf-8")

    assert CompatMemory(path).lookup(KEY, "11.0.7.61") is None
