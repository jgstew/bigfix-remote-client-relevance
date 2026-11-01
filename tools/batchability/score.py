"""Score the classifier against what qna actually did.

Needs the analyzer (the classifier) but not qna::

    uv run --no-project --python 3.12 --exclude-newer-package 'bigfix-relevance-analyzer=0 days' \
        --with 'bigfix-relevance-analyzer==1.43.0' python tools/batchability/score.py tools/batchability/corpus.jsonl \
        tools/batchability/results/*.jsonl > tools/batchability/scores.md

**Actual outcome**, per (expression, host):

- ``one`` -- exactly one answer, no error, and tupled with a sentinel it
  came back as exactly one answer that splits on ``, `` into the expression's
  answer and the sentinel, with no part holding ``,``, ``(`` or ``)`` (the
  characters that make an item display wrapped). This is "batchable".
- ``error`` / ``empty`` / ``plural`` -- what went wrong on its own;
  ``env-error`` when the container itself was the cause (no SMBIOS tables,
  no RPM inspector), which no real endpoint shares.
- ``split`` -- one clean answer, but not one a tuple can carry.

Rows are split into a **dev** half (even ids), which the rules were tuned on,
and a **holdout** half (odd ids), which they were not.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from batchability import batchability  # type: ignore[import-not-found]

SENTINEL = "bfrcr-78-sentinel"
# run_spike.py's --host names: a label to publish, and the analyzer platform.
HOSTS = {
    "jgstews-mac-mini.local": ("macOS 15.7 x86_64 (SSH, installed agent)", "macos"),
    "rockylinux-9-arm64": ("Rocky Linux 9.3 arm64 (container)", "rhel"),
    "ubuntu-26-04-arm64": ("Ubuntu 26.04 arm64 (container)", "ubuntu"),
    "windows-server-2022": ("Windows Server 2022 x86_64 (SSH, installed agent)", "windows"),
}
PLATFORMS = {host: platform for host, (_, platform) in HOSTS.items()}
_WRAP_MARKERS = (",", "(", ")")


def splits(answer: str, parts: int) -> list[str] | None:
    """The tuple answer's items, or None when it cannot be split for sure."""
    items = answer.split(", ")
    if len(items) != parts or any(m in item for item in items for m in _WRAP_MARKERS):
        return None
    return items


# Errors from the container itself, not the relevance: a container has no
# SMBIOS/DMI tables, and Rocky's image cannot set up the RPM inspector. A real
# endpoint has neither problem.
CONTAINER_ERRORS = ("no SMBIOS/DMI tables", "RPM inspector failed to create symlink")


def outcome(record: dict) -> str:  # type: ignore[type-arg]
    single, tupled = record["single"], record["tuple"]
    if single["error_kind"] is not None:
        if any(text in (single["error"] or "") for text in CONTAINER_ERRORS):
            return "env-error"
        return "error"
    answers = single["answers"]
    # qna answers an empty plural with no answers at all (Fast Query with one
    # empty row, which split_tuple_answer refuses just the same).
    if not answers:
        return "empty"
    if len(answers) > 1:
        return "plural"
    if tupled["error_kind"] is not None or len(tupled["answers"]) != 1:
        return "split"
    items = splits(tupled["answers"][0], 2)
    if items is None or items != [answers[0], SENTINEL]:
        return "split"
    return "one"


# Operators that answer one boolean from singular operands.
_BOOLEAN_OPS = frozenset(
    {"=", "!=", "<", ">", "<=", ">=", "and", "or", "contains", "starts with", "ends with"}
    | {"does not contain", "does not start with", "does not end with", "is", "is not"}
    | {"is equal to", "is not equal to", "is greater than", "is less than"}
)


def _boolean_shape(text: str) -> bool:
    """The shape rule alone: the root is `exists`, `not exists`, `number of`,
    a comparison or `and` / `or` / `not` -- what applicability relevance
    usually is -- with no type checking at all."""
    from bigfix_relevance_analyzer.analyzer import analyze
    from bigfix_relevance_analyzer.nodes import Binary, Exists, NumberOf, Unary

    node = analyze(text).node
    match node:
        case Exists() | NumberOf():
            return True
        case Unary(op=op):
            return op.lower() == "not"
        case Binary(op=op):
            return op.lower() in _BOOLEAN_OPS
        case _:
            return False


def precision(tp: int, fp: int) -> str:
    return f"{tp / (tp + fp):.1%}" if tp + fp else "n/a"


def main(corpus_path: str, *result_paths: str) -> None:
    corpus = {row["id"]: row for row in map(json.loads, Path(corpus_path).read_text().splitlines())}
    predictors = {
        "classifier (platform-agnostic)": lambda text, platform: batchability(text).safe,
        "classifier (platform known)": lambda text, platform: (
            batchability(text, platform=platform).safe
        ),
        "plurality == SINGULAR only": lambda text, platform: (
            batchability(text).plurality.value == "singular"
        ),
        "shape only (exists / number of / comparison / and-or-not at the root)": (
            lambda text, platform: _boolean_shape(text)
        ),
    }
    verdict_cache: dict[tuple[str, str | None], object] = {}

    def predicted(name: str, text: str, platform: str | None) -> bool:
        key = (name + "\0" + text, platform)
        if key not in verdict_cache:
            verdict_cache[key] = predictors[name](text, platform)
        return bool(verdict_cache[key])

    rows = []
    for path in result_paths:
        for record in map(json.loads, Path(path).read_text().splitlines()):
            record["label"] = HOSTS[record["host"]][0]
            rows.append((record, corpus[record["id"]], outcome(record)))

    print("# Batchability spike scores\n")  # noqa: T201
    print("## Actual outcomes per host\n")  # noqa: T201
    print("| host | qna | total | one | error | env-error | empty | plural | split |")  # noqa: T201
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|")  # noqa: T201
    by_host: dict[str, list] = collections.defaultdict(list)  # type: ignore[type-arg]
    for record, row, actual in rows:
        by_host[record["label"]].append((record, row, actual))
    for host, items in by_host.items():
        counts = collections.Counter(actual for _, _, actual in items)
        print(  # noqa: T201
            f"| {host} | {items[0][0]['qna_version']} | {len(items)} | "
            + " | ".join(
                str(counts[k]) for k in ("one", "error", "env-error", "empty", "plural", "split")
            )
            + " |"
        )

    print('\n## Precision of a "safe to batch" verdict\n')  # noqa: T201
    print(  # noqa: T201
        "Container environment errors (no SMBIOS, Rocky's RPM symlink) are left out "
        "of every count below; the raw counts are in the table above.\n"
    )
    print(  # noqa: T201
        "| predictor | half | host | predicted safe | actually one | precision | recall |"
    )
    print("|---|---|---|---:|---:|---:|---:|")  # noqa: T201
    rows = [(record, row, actual) for record, row, actual in rows if actual != "env-error"]
    false_safe: dict[str, list] = collections.defaultdict(list)  # type: ignore[type-arg]
    record_error = {(r["label"], r["id"]): r["single"]["error"] for r, _, _ in rows}
    for name in predictors:
        for half, parity in (("dev", 0), ("holdout", 1), ("all", None)):
            for host in [*by_host, "all hosts"]:
                tp = fp = positives = 0
                for record, row, actual in rows:
                    if host != "all hosts" and record["label"] != host:
                        continue
                    if parity is not None and int(row["id"][1:]) % 2 != parity:
                        continue
                    platform = PLATFORMS.get(record["host"])
                    safe = predicted(name, row["text"], platform)
                    positives += actual == "one"
                    if safe and actual == "one":
                        tp += 1
                    elif safe:
                        fp += 1
                        if half == "all" and host == "all hosts":
                            false_safe[name].append((record["label"], actual, row))
                recall = f"{tp / positives:.1%}" if positives else "n/a"
                print(  # noqa: T201
                    f"| {name} | {half} | {host} | {tp + fp} | {tp} | {precision(tp, fp)} "
                    f"| {recall} |"
                )

    print("\n## Classifier verdicts against actual outcomes (all hosts)\n")  # noqa: T201
    table: collections.Counter[tuple[str, str]] = collections.Counter()
    for record, row, actual in rows:
        kind = batchability(row["text"], platform=PLATFORMS.get(record["host"])).kind
        table[(kind, actual)] += 1
    outcomes = ("one", "error", "empty", "plural", "split")
    print("| verdict (platform known) | " + " | ".join(outcomes) + " |")  # noqa: T201
    print("|---|" + "---:|" * len(outcomes))  # noqa: T201
    for kind in ("safe", "split_risk", "plural_risk", "unknown", "error_risk"):
        print(  # noqa: T201
            f"| {kind} | " + " | ".join(str(table[(kind, o)]) for o in outcomes) + " |"
        )

    for name, items in false_safe.items():
        if not name.startswith("classifier"):
            continue
        print(f'\n## False "safe": {name} ({len(items)})\n')  # noqa: T201
        for host, actual, row in items:
            error = (record_error.get((host, row["id"])) or "")[:70]
            print(f"- {host}: {actual} `{row['id']}` `{row['text'][:120]}` {error}")  # noqa: T201


if __name__ == "__main__":
    main(sys.argv[1], *sys.argv[2:])
