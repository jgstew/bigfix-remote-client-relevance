"""What the platform this library runs on can do, for transports and discovery.

iOS and Android apps embed this package (pyproject.toml leaves asyncssh and
zstandard out there). Neither can run qna: iOS has no subprocesses at all, and
an Android app can't run a downloaded Linux binary. Without this check,
first-run discovery on an iPhone downloaded an Ubuntu agent package before
``subprocess`` failed with ``[Errno 45] ios does not support processes``.
"""

from __future__ import annotations

import sys

NO_PROCESS_PLATFORMS = frozenset({"ios", "tvos", "watchos", "android", "emscripten", "wasi"})
"""``sys.platform`` values where the local and container transports can't work.

CPython has no public "can this platform start processes?" check
(``subprocess._can_fork_exec`` is private), so this tracks the platforms it
ships without ``fork``/``exec``. ``android`` can start processes, but not run
qna, and container engines don't apply there."""


def can_run_processes() -> bool:
    """False where CPython can't start qna (mobile, WASM): no local qna, no docker CLI."""
    return sys.platform not in NO_PROCESS_PLATFORMS


def no_process_advice(what: str) -> str:
    """One message for anything that needs a process here and can't have one."""
    return (
        f"{what} needs to start qna, which {sys.platform} can't; use fastquery or online_evaluator"
    )


__all__ = ["NO_PROCESS_PLATFORMS", "can_run_processes", "no_process_advice"]
