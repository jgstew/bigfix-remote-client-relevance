"""Tests for sharing per-image work across a fan-out.

A transport is built per (target, version) pair, so a sweep over four versions
of one image would otherwise pull it four times and build the same prepared
image four times.
"""

from __future__ import annotations

import asyncio

import pytest

from bigfix_remote_client_relevance.transports.coordination import ImageCoordinator


def counting_factory(calls: list[str], key: str, *, result: str = "done", delay: float = 0.0):
    async def work() -> str:
        if delay:
            await asyncio.sleep(delay)
        calls.append(key)
        return result

    return work


async def test_concurrent_callers_share_one_run():
    calls: list[str] = []
    coordinator = ImageCoordinator()

    results = await asyncio.gather(
        *(
            coordinator.once("ubuntu", counting_factory(calls, "ubuntu", delay=0.01))
            for _ in range(5)
        )
    )

    assert calls == ["ubuntu"], "five callers, one pull"
    assert results == ["done"] * 5, "and everyone gets the answer"


async def test_a_later_caller_reuses_the_finished_result():
    calls: list[str] = []
    coordinator = ImageCoordinator()

    await coordinator.once("ubuntu", counting_factory(calls, "ubuntu"))
    await coordinator.once("ubuntu", counting_factory(calls, "ubuntu"))

    assert calls == ["ubuntu"]


async def test_distinct_keys_are_not_shared():
    calls: list[str] = []
    coordinator = ImageCoordinator()

    await asyncio.gather(
        coordinator.once("a", counting_factory(calls, "a")),
        coordinator.once("b", counting_factory(calls, "b")),
    )

    assert sorted(calls) == ["a", "b"]


async def test_a_failure_reaches_every_caller():
    coordinator = ImageCoordinator()
    attempts: list[int] = []

    async def failing() -> str:
        attempts.append(1)
        raise RuntimeError("no such image")

    async def ask():
        with pytest.raises(RuntimeError, match="no such image"):
            await coordinator.once("ubuntu", failing)

    await asyncio.gather(ask(), ask(), ask())

    assert len(attempts) == 1, "a doomed pull is not retried once per target"


async def test_one_key_failing_does_not_poison_another():
    coordinator = ImageCoordinator()

    async def failing() -> str:
        raise RuntimeError("no such image")

    with pytest.raises(RuntimeError):
        await coordinator.once("broken", failing)

    calls: list[str] = []
    assert await coordinator.once("fine", counting_factory(calls, "fine")) == "done"


async def test_separate_coordinators_do_not_share():
    """Scoped per run, so a registry never outlives the event loop it belongs to."""
    calls: list[str] = []

    await ImageCoordinator().once("ubuntu", counting_factory(calls, "ubuntu"))
    await ImageCoordinator().once("ubuntu", counting_factory(calls, "ubuntu"))

    assert calls == ["ubuntu", "ubuntu"]
