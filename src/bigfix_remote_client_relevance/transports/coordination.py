"""Sharing expensive per-image work across a fan-out.

A transport is built per (target, version) pair, so four versions of one image
mean four transports, four pulls of the same image, and four builds of the same
prepared image. This coordinates them: the first caller for a key does the
work, everyone else waits on that same result.

Memoizing an :class:`asyncio.Task` rather than holding an :class:`asyncio.Lock`
is deliberate. A Lock binds to the event loop that first acquires it, so a
module-level registry of them breaks the moment a second loop reuses a key —
which is exactly what a test suite does. Creating the task is synchronous and
happens before any await, so the dict cannot race, and a coordinator scoped to
one run never outlives its loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class ImageCoordinator:
    """Runs work once per key, for everyone who asks."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    async def once(self, key: str, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Await the shared result for ``key``, starting the work if needed.

        A failure is memoized along with everything else: the callers sharing
        a key share its outcome, and a doomed pull is not retried once per
        target.
        """
        raise NotImplementedError


__all__ = ["ImageCoordinator"]
