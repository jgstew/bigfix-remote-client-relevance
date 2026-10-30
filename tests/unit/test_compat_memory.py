"""Tests for remembering which qna builds are too new for a target.

Without this, every run against an old-runtime target pays for one doomed
attempt at the newest build before falling back.
"""

from __future__ import annotations

import json
import os

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


# The orchestrator looks up once per pinned (target, version) group, so a
# 500-target run read and parsed the whole file 500 times -- about half the
# run's time. The parsed file is cached for as long as the file is unchanged.


def _count_reads(monkeypatch):
    from pathlib import Path

    reads = []
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return reads


def test_repeated_lookups_read_the_file_once(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    reads = _count_reads(monkeypatch)
    memory = CompatMemory(path)

    for _ in range(50):
        assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    assert len(reads) == 1


def test_a_file_replaced_by_another_process_is_picked_up(tmp_path):
    path = tmp_path / "m.json"
    memory = CompatMemory(path)
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    # Another run, with its own instance, writes a different fallback.
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.5.204")
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.5.204"

    # ...and another forgets it.
    CompatMemory(path).forget(KEY)
    assert memory.lookup(KEY, "11.0.7.61") is None


def test_a_same_size_rewrite_within_one_timestamp_is_picked_up(tmp_path):
    """Write-then-rename gives the file a new inode even when size and
    mtime match, which coarse-timestamp filesystems make likely."""
    path = tmp_path / "m.json"
    memory = CompatMemory(path, clock=lambda: 1_000_000.0)
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"
    before = path.stat()

    CompatMemory(path, clock=lambda: 1_000_000.0).record(
        KEY, too_new="11.0.7.61", works="11.0.6.138"
    )
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.138"


def test_a_deleted_file_forgets_everything(tmp_path):
    path = tmp_path / "m.json"
    memory = CompatMemory(path)
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    path.unlink()

    assert memory.lookup(KEY, "11.0.7.61") is None


def test_an_unreadable_file_reads_as_empty(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "m.json"
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    def unreadable(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", unreadable)
    memory = CompatMemory(path)

    assert memory.lookup(KEY, "11.0.7.61") is None
    assert memory.lookup(KEY, "11.0.7.61") is None


def test_record_then_lookup_needs_no_reread(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    memory = CompatMemory(path)
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    reads = _count_reads(monkeypatch)

    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"
    memory.forget(KEY)
    assert memory.lookup(KEY, "11.0.7.61") is None

    assert len(reads) == 1  # forget's read-modify-write, against the file


def test_record_merges_with_what_another_process_wrote(tmp_path):
    """record must read the current file, not a cached copy, or it would
    drop the other run's entry when it saves."""
    path = tmp_path / "m.json"
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") is None  # caches "empty"

    CompatMemory(path).record("other", too_new="11.0.7.61", works="11.0.6.137")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert CompatMemory(path).lookup("other", "11.0.7.61") == "11.0.6.137"
