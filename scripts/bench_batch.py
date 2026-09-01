"""Measure what batching client-relevance evaluations actually saves.

The premise of ``evaluate_many`` is that sharing the setup is cheaper than
repaying it per expression. This measures it rather than asserting it, three
ways against one image:

1. **cold**       -- ``evaluate_client_relevance`` once per expression, a
                     fresh transport each time. What callers do today.
2. **batched**    -- ``evaluate_many``, one-shot containers. Shares the image
                     work and the resolution, not the container.
3. **kept**       -- ``evaluate_many`` against a kept-alive target: one
                     container for the whole batch, N execs.

Run it against a real engine::

    uv run python scripts/bench_batch.py --image ubuntu:22.04 --qna-version 11.0

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
from bigfix_remote_client_relevance.transports.container import reclaim_stray_containers

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

    async def kept() -> list[object]:
        return list(
            await evaluate_many(expressions, [target(image, arch, keep_alive=True)], **kwargs)
        )

    await reclaim_stray_containers()
    return {
        "cold": await timed("cold", cold),
        "batched": await timed("batched", batched),
        "kept": await timed("kept", kept),
    }


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
    header = f"{'N':>3}  {'cold':>9}  {'batched':>9}  {'kept':>9}   saving"
    print(header)  # noqa: T201
    print("-" * len(header))  # noqa: T201

    for count in counts:
        runs = [
            await measure(args.image, args.arch, args.qna_version, count)
            for _ in range(args.repeat)
        ]
        median = {key: statistics.median(run[key] for run in runs) for key in runs[0]}
        speedup = median["cold"] / median["kept"] if median["kept"] else 0
        print(  # noqa: T201
            f"{count:>3}  {median['cold']:>9.0f}  {median['batched']:>9.0f}  "
            f"{median['kept']:>9.0f}   "
            f"{median['cold'] - median['kept']:>7.0f}ms ({speedup:.1f}x)"
        )


if __name__ == "__main__":
    asyncio.run(main())
