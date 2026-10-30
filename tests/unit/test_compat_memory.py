"""Tests for remembering which qna builds are too new for a target.

Without this, every run against an old-runtime target pays for one doomed
attempt at the newest build before falling back.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

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


def _count_parses(monkeypatch):
    parses = []
    real_loads = json.loads

    def loads(*args, **kwargs):
        parses.append(args)
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    return parses


def _age(path, seconds_ago=3600):
    """Backdate ``path``: a file written moments ago is never trusted to the
    cache, since a same-size rewrite within one timestamp tick looks alike."""
    then = time.time() - seconds_ago
    os.utime(path, (then, then))


def _fail_reads(monkeypatch):
    real_open = Path.open

    def open_(self, mode="r", *args, **kwargs):
        if "r" in mode:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)


def test_repeated_lookups_read_the_file_once(tmp_path, monkeypatch):
    path = tmp_path / "m.json"
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    parses = _count_parses(monkeypatch)
    memory = CompatMemory(path)

    for _ in range(50):
        assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    assert len(parses) == 1


def test_a_file_replaced_by_another_process_is_picked_up(tmp_path):
    path = tmp_path / "m.json"
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    # Another run, with its own instance, writes a different fallback.
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.5.204")
    _age(path)
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.5.204"

    # ...and another forgets it.
    CompatMemory(path).forget(KEY)
    _age(path)
    assert memory.lookup(KEY, "11.0.7.61") is None


def test_a_same_size_rewrite_with_the_same_timestamp_is_picked_up(tmp_path):
    """Write-then-rename gives the file a new inode even when size and
    mtime match, which coarse-timestamp filesystems make likely."""
    path = tmp_path / "m.json"
    now = time.time()  # one recorded_at for both writes, so sizes match
    CompatMemory(path, clock=lambda: now).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"
    before = path.stat()

    CompatMemory(path, clock=lambda: now).record(KEY, too_new="11.0.7.61", works="11.0.6.138")
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.138"


def test_a_fresh_file_rewritten_in_place_is_picked_up(tmp_path):
    """Same inode, size and timestamp tick -- or a freed inode number reused
    within the tick -- is indistinguishable by stat, so a file that recent
    is never served from the cache."""
    path = tmp_path / "m.json"
    now = time.time()
    CompatMemory(path, clock=lambda: now).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"
    before = path.stat()

    path.write_text(path.read_text(encoding="utf-8").replace("6.137", "6.138"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_ino == before.st_ino

    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.138"


def test_a_deleted_file_forgets_everything(tmp_path):
    path = tmp_path / "m.json"
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    path.unlink()

    assert memory.lookup(KEY, "11.0.7.61") is None


def test_an_unreadable_file_reads_as_empty_until_it_is_readable(tmp_path, monkeypatch):
    """A momentary lock (antivirus, a backup) must not stick: the next
    lookup retries, as it did before there was a cache."""
    path = tmp_path / "m.json"
    CompatMemory(path).record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    memory = CompatMemory(path)

    with monkeypatch.context() as patch:
        _fail_reads(patch)
        assert memory.lookup(KEY, "11.0.7.61") is None
        assert memory.lookup(KEY, "11.0.7.61") is None

    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"


def test_an_impossible_path_is_nothing_remembered(tmp_path):
    memory = CompatMemory(tmp_path / "m\0.json")

    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    memory.forget(KEY)
    assert memory.lookup(KEY, "11.0.7.61") is None


def test_record_then_lookup_in_one_instance(tmp_path):
    path = tmp_path / "m.json"
    CompatMemory(path).record("other", too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") is None  # cached, without KEY

    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")
    _age(path)
    assert memory.lookup(KEY, "11.0.7.61") == "11.0.6.137"

    memory.forget(KEY)
    _age(path)
    assert memory.lookup(KEY, "11.0.7.61") is None


def test_record_merges_with_what_another_process_wrote(tmp_path):
    """record must read the current file, not a cached copy, or it would
    drop the other run's entry when it saves."""
    path = tmp_path / "m.json"
    path.write_text("{}", encoding="utf-8")
    _age(path)
    memory = CompatMemory(path)
    assert memory.lookup(KEY, "11.0.7.61") is None  # caches "empty"

    CompatMemory(path).record("other", too_new="11.0.7.61", works="11.0.6.137")
    memory.record(KEY, too_new="11.0.7.61", works="11.0.6.137")

    assert CompatMemory(path).lookup("other", "11.0.7.61") == "11.0.6.137"


def test_a_pathologically_nested_file_reads_as_empty(tmp_path):
    """json raises RecursionError, not ValueError, on deep enough nesting."""
    path = tmp_path / "m.json"
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")

    assert CompatMemory(path).lookup(KEY, "11.0.7.61") is None
