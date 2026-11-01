"""Evaluate the corpus for real: each expression alone, and tupled with a sentinel.

Runs in this repo's own environment (no analyzer needed)::

    uv run python tools/batchability/run_spike.py --host debian-12 \
        tools/batchability/corpus.jsonl tools/batchability/results/debian-12.jsonl

``--host`` names an entry in the inventory (``~/.bigfix/remote_clients.toml``
by default; ``--inventory`` to change it). Read-only: the inventory is never
written back. Every expression is evaluated twice through the library's
normal qna-batch path:

- ``E`` on its own: the answer count, the answers, and any error;
- ``((E), "bfrcr-78-sentinel")``: what a tuple batch would see -- the count
  and whether its one answer is ``<E's answer>, bfrcr-78-sentinel``.

One JSON object per corpus row, keyed by its ``id``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from bigfix_remote_client_relevance.inventory import load_inventory
from bigfix_remote_client_relevance.orchestrate import evaluate_many

SENTINEL = "bfrcr-78-sentinel"


def tupled(text: str) -> str:
    return f'(({text}), "{SENTINEL}")'


def _outcome(result: object) -> dict[str, object]:
    return {
        "answers": list(result.answers),  # type: ignore[attr-defined]
        "types": list(result.answer_types),  # type: ignore[attr-defined]
        "error": result.error,  # type: ignore[attr-defined]
        "error_kind": result.error_kind,  # type: ignore[attr-defined]
        "elapsed_ms": result.elapsed_ms,  # type: ignore[attr-defined]
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus")
    parser.add_argument("out")
    parser.add_argument("--host", required=True)
    parser.add_argument("--inventory", default=str(Path("~/.bigfix/remote_clients.toml")))
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    targets = [
        target
        for target in load_inventory(Path(args.inventory).expanduser())
        if target.extra.get("host") == args.host or target.name == args.host
    ]
    if len(targets) != 1:
        raise SystemExit(f"{args.host!r} matched {len(targets)} inventory entries")
    rows = [json.loads(line) for line in Path(args.corpus).read_text().splitlines() if line]
    expressions = [row["text"] for row in rows] + [tupled(row["text"]) for row in rows]
    results = await evaluate_many(expressions, targets, timeout_s=args.timeout)
    if len(results) != len(expressions):
        raise SystemExit(f"{len(results)} results for {len(expressions)} expressions")
    singles, tuples = results[: len(rows)], results[len(rows) :]

    records = []
    for row, single, tuple_result in zip(rows, singles, tuples, strict=True):
        expected = (
            f"{single.answers[0]}, {SENTINEL}"
            if len(single.answers) == 1 and single.error_kind is None
            else None
        )
        record = {
            "id": row["id"],
            "host": args.host,
            "qna_version": single.qna_version,
            "platform": single.platform,
            "single": _outcome(single),
            "tuple": _outcome(tuple_result),
            "tuple_ok": (
                tuple_result.error_kind is None
                and len(tuple_result.answers) == 1
                and tuple_result.answers[0] == expected
            ),
        }
        records.append(record)
    _write(Path(args.out), records)


def _write(out: Path, records: list[dict[str, object]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(record) + "\n" for record in records))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
