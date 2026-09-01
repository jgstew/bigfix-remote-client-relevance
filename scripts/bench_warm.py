"""Measure what a batched, warm container actually saves.

The premise of ``evaluate_many`` and the idle deadline is that reuse is
cheaper than starting over. This measures it rather than asserting it, four
ways against one image:

1. **cold**       -- ``evaluate_client_relevance`` once per expression, a
                     fresh transport each time. What callers do today.
2. **batched**    -- ``evaluate_many``, one-shot containers. Shares the image
                     work and the resolution, not the container.
3. **warm**       -- ``evaluate_many`` against a kept-alive target: one
                     container, N execs.
4. **adopted**    -- a second ``evaluate_many`` that finds the container the
                     third left behind. The cross-process case, which is what
                     a script running every few minutes actually hits.

Run it against a real engine::

    uv run python scripts/bench_warm.py --image ubuntu:22.04 --qna-version 11.0

Not shipped in the wheel (``[tool.hatch.build]`` scopes that to ``src/``).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence

from bigfix_remote_client_relevance import (
    Target,
    evaluate_client_relevance,
    evaluate_many,
)
from bigfix_remote_client_relevance.transports.container import stop_warm_containers

EXPRESSIONS = [
    "name of operating system",
    "version of client",
    "number of properties",
    "computer name",
    "architecture of operating system",
    "current date",
    'number of files of folder "/etc"',
    "number of processes",
    "number of drives",
    "boot time of operating system",
]


def target(image: str, arch: str, *, keep_alive: bool) -> Target:
    return Target(
        kind="container",
        name=image,
        image=image,
        arch=arch,
        keep_alive=keep_alive,
    )


async def timed(label: str, run: Callable[[], Awaitable[Sequence[object]]]) -> float:
    started = time.monotonic()
    results = await run()
    elapsed = (time.monotonic() - started) * 1000
    failures = [r for r in results if getattr(r, "error_kind", None) is not None]
    if failures:
        first = failures[0]
        print(f"    ! {label}: {len(failures)}/{len(results)} failed -- {first.error}")  # noqa: T201
    return elapsed


async def measure(image: str, arch: str, version: str | None, count: int) -> dict[str, float]:
    expressions = EXPRESSIONS[:count]
    kwargs = {"qna_version": version}

    async def cold() -> list[object]:
        out: list[object] = []
        for expression in expressions:
            out.extend(
                await evaluate_client_relevance(
                    expression, [target(image, arch, keep_alive=False)], **kwargs
                )
            )
        return out

    async def batched() -> list[object]:
        return list(
            await evaluate_many(expressions, [target(image, arch, keep_alive=False)], **kwargs)
        )

    async def warm() -> list[object]:
        return list(
            await evaluate_many(expressions, [target(image, arch, keep_alive=True)], **kwargs)
        )

    await stop_warm_containers()
    timings = {
        "cold": await timed("cold", cold),
        "batched": await timed("batched", batched),
        "warm": await timed("warm", warm),
    }
    # The container the warm run left behind is still there; this run adopts it.
    timings["adopted"] = await timed("adopted", warm)
    await stop_warm_containers()
    return timings


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ubuntu:22.04")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--qna-version", default=None)
    parser.add_argument("--counts", default="1,3,10")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    counts = [int(part) for part in args.counts.split(",")]
    print(f"# {args.image} @ {args.arch}, qna {args.qna_version or 'whatever is installed'}")  # noqa: T201
    print(f"# medians of {args.repeat} run(s), milliseconds\n")  # noqa: T201
    header = f"{'N':>3}  {'cold':>9}  {'batched':>9}  {'warm':>9}  {'adopted':>9}   saving"
    print(header)  # noqa: T201
    print("-" * len(header))  # noqa: T201

    for count in counts:
        runs = [
            await measure(args.image, args.arch, args.qna_version, count)
            for _ in range(args.repeat)
        ]
        median = {key: statistics.median(run[key] for run in runs) for key in runs[0]}
        speedup = median["cold"] / median["adopted"] if median["adopted"] else 0
        print(  # noqa: T201
            f"{count:>3}  {median['cold']:>9.0f}  {median['batched']:>9.0f}  "
            f"{median['warm']:>9.0f}  {median['adopted']:>9.0f}   "
            f"{median['cold'] - median['adopted']:>7.0f}ms ({speedup:.1f}x)"
        )


if __name__ == "__main__":
    asyncio.run(main())
