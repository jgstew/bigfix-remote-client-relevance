"""Remember which qna builds are too new for a target, across runs.

A target whose runtime is older than a release's build (debian:11 arm64 against
11.0.7.61, which needs GLIBC_2.38) falls back to an older release -- but only
after one doomed attempt at the new build. Recording the outcome lets later
runs go straight to the version that worked.

Kept in the platform *state* directory beside the prereq cache, not in the
artifact cache, which is safe to wipe. Entries expire so a target whose
runtime was upgraded is eventually re-checked against the newer build.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

import platformdirs

logger = logging.getLogger(__name__)

APP_NAME = "bigfix_remote_client_relevance"

DEFAULT_TTL_S = 30 * 24 * 60 * 60

# Coarser than any common filesystem's timestamp tick (FAT's is 2 s).
_RACY_NS = 3_000_000_000

_FULL_VERSION = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def default_path() -> Path:
    return Path(platformdirs.user_state_dir(APP_NAME)) / "compat_memory.json"


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _stamp(st: os.stat_result) -> tuple[int, int, int]:
    # The inode catches a same-size rewrite within one timestamp tick:
    # _save's rename always puts a new inode at the path.
    return (st.st_ino, st.st_size, st.st_mtime_ns)


class CompatMemory:
    """``{target key: (lowest too-new version, version that worked)}``, on disk.

    Every failure to read or write is logged and treated as "nothing
    remembered": this only ever saves time, so it must never fail a run.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path or default_path()
        self._ttl_s = ttl_s
        self._clock = clock
        # The orchestrator looks up once per pinned target, so re-parsing
        # the file each time was half of a large run's time. Cached against
        # the file's stamp, so another run's write is still seen.
        self._cached: tuple[tuple[int, int, int], dict[str, object]] | None = None

    def lookup(self, key: str, version: str) -> str | None:
        """The version to use instead of ``version`` on ``key``, if known.

        Anything at or above the recorded too-new version is redirected:
        runtime requirements only grow from one release to the next.
        """
        entry = self._load_cached().get(key)
        if not isinstance(entry, dict):
            return None
        too_new, works, at = entry.get("too_new"), entry.get("works"), entry.get("recorded_at")
        if not (
            isinstance(too_new, str)
            and isinstance(works, str)
            and isinstance(at, (int, float))
            and _FULL_VERSION.match(too_new)
            and _FULL_VERSION.match(works)
        ):
            return None
        if self._clock() - at > self._ttl_s:
            return None
        if _key(version) < _key(too_new) or _key(works) >= _key(version):
            return None
        return works

    def record(self, key: str, *, too_new: str, works: str) -> None:
        entries = self._load()
        previous = entries.get(key)
        if isinstance(previous, dict) and isinstance(previous.get("too_new"), str):
            try:
                if _key(previous["too_new"]) < _key(too_new):
                    too_new = previous["too_new"]
            except ValueError:
                pass  # a malformed old entry is simply replaced
        entries[key] = {"too_new": too_new, "works": works, "recorded_at": self._clock()}
        self._save(entries)

    def forget(self, key: str) -> None:
        entries = self._load()
        if entries.pop(key, None) is not None:
            self._save(entries)

    def _load_cached(self) -> dict[str, object]:
        try:
            stamp = _stamp(self._path.stat())
        except (OSError, ValueError):
            return {}  # missing, no access, or no such path: nothing remembered
        if self._cached is not None and self._cached[0] == stamp:
            return self._cached[1]
        read = self._read()
        if read is None:
            return {}  # not cached, so a momentary lock is retried next time
        st, entries = read
        # git's "racily clean" rule: a rewrite within one timestamp tick, or a
        # freed inode number reused within it, can leave the stamp unchanged,
        # so a file that recent is re-read rather than trusted.
        if abs(time.time_ns() - st.st_mtime_ns) > _RACY_NS:
            self._cached = (_stamp(st), entries)
        return entries

    def _load(self) -> dict[str, object]:
        read = self._read()
        return read[1] if read is not None else {}

    def _read(self) -> tuple[os.stat_result, dict[str, object]] | None:
        # Stat and read one open file, so the stamp is for what was parsed
        # even if another run replaces the file in between.
        try:
            with self._path.open(encoding="utf-8") as handle:
                st = os.fstat(handle.fileno())
                data = json.loads(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError) as exc:
            logger.debug("ignoring unreadable compat memory %s: %s", self._path, exc)
            return None
        return st, data if isinstance(data, dict) else {}

    def _save(self, entries: dict[str, object]) -> None:
        # Write-then-rename, so a concurrent reader never sees half a file.
        # The next lookup re-reads: the partial name is shared by threads, so
        # what lands at the path may not be exactly what this call wrote.
        self._cached = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            partial = self._path.with_suffix(f".{os.getpid()}.partial")
            partial.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            partial.replace(self._path)
        except (OSError, ValueError) as exc:
            logger.debug("could not save compat memory %s: %s", self._path, exc)


__all__ = ["DEFAULT_TTL_S", "CompatMemory", "default_path"]
