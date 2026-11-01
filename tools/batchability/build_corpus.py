"""Build the spike corpus: real client relevance plus hand-picked edge cases.

Run with the analyzer available (it does the extraction)::

    uv run --no-project --python 3.12 --exclude-newer-package 'bigfix-relevance-analyzer=0 days' \
        --with 'bigfix-relevance-analyzer==1.43.0' python tools/batchability/build_corpus.py ~/Projects/bigfix-content \
        > tools/batchability/corpus.jsonl

One JSON object per line: ``{"id", "source", "kind", "text"}``. Texts are
deduplicated (whitespace-normalized); the 751 near-identical Dell BIOS
fixlets are capped so they do not swamp everything else.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

from bigfix_relevance_analyzer import Dialect, extract_relevance_from_file

# Kinds evaluated on an endpoint as a whole statement. Actionscript
# substitutions and conditions are client relevance too, but often depend on
# action parameters that do not exist outside an action.
KINDS = {"relevance", "analysis-property", "global-property"}
PER_DIRECTORY_CAP = 40
MAX_LENGTH = 4000
SEED = 78

# Edge cases the corpus would otherwise be thin on, each named for what it
# probes. Several spell the same thing singular and plural on purpose.
EDGE_CASES: dict[str, str] = {
    "literal-true": "true",
    "literal-int": "42",
    "literal-string-comma": '"a, b"',
    "literal-string-empty": '""',
    "literal-string-trailing-space": '"x "',
    "os-name": "name of operating system",
    "computer-name": "computer name",
    "os-version": "version of operating system",
    "client-version": "version of client",
    "now": "now",
    "tuple-literal": '(1, "a")',
    "tuple-of-inspectors": "(name of operating system, computer name)",
    "plural-literal": "(1;2;3)",
    "plural-one-value": "(1;1) whose (it = 1)",
    "plural-empty": "(1;2) whose (it > 5)",
    "exists-empty": "exists (1;2) whose (it > 5)",
    "not-exists-empty": "not exists (1;2) whose (it > 5)",
    "number-of-empty": "number of (1;2) whose (it > 5)",
    "singular-missing-file": 'size of file "/nonexistent/bfrcr-78"',
    "plural-missing-file": 'sizes of files "/nonexistent/bfrcr-78"',
    "exists-missing-file": 'exists file "/nonexistent/bfrcr-78"',
    "singular-missing-folder-mid-chain": 'names of files of folder "/nonexistent/bfrcr-78"',
    "plural-missing-folder-mid-chain": 'names of files of folders "/nonexistent/bfrcr-78"',
    "bar-fallback": 'size of file "/nonexistent/bfrcr-78" | 0',
    "if-then-else": 'if exists file "/nonexistent/bfrcr-78" then "yes" else "no"',
    "if-singular-branch-errors": 'if true then size of file "/nonexistent/bfrcr-78" else 0',
    "and-short-circuit": 'false and size of file "/nonexistent/bfrcr-78" > 0',
    "or-short-circuit": 'true or size of file "/nonexistent/bfrcr-78" > 0',
    "comparison-plural-lhs": "(1;2;3) > 2",
    "comparison-plural-lhs-empty": "((1;2) whose (it > 5)) = 1",
    "unique-values": "unique values of (1;1;2)",
    "multiplicity": "multiplicities of unique values of (1;1;2)",
    "concatenation": 'concatenation ", " of ("a";"b")',
    "concatenation-empty": 'concatenation of ((("a";"b")) whose (it = "z"))',
    "string-with-comma": '"x" & ", " & "y"',
    "item-of-tuple": 'item 1 of (1, "a")',
    "plural-items": 'items 1 of (1, "a")',
    "maximum-empty": "maximum of (1;2) whose (it > 5)",
    "maximum": "maximum of (1;2;3)",
    "error-unknown-inspector": "bfrcr nonexistent inspector",
    "error-type": '1 + "a"',
    "error-singular-required": '(1;2) as string & ""',
    "environment-variable": 'value of variable "PATH" of environment',
    "environment-variable-missing": 'value of variable "BFRCR_78_MISSING" of environment',
    "environment-variables-missing": 'values of variables "BFRCR_78_MISSING" of environment',
    "windows-only": 'exists key "HKLM\\SOFTWARE" of native registry',
    "windows-guarded": (
        'if windows of operating system then exists key "HKLM\\SOFTWARE" of '
        "native registry else false"
    ),
    "macos-only-guarded": (
        'if mac of operating system then exists folder "/Applications" else false'
    ),
    "files-of-tmp": 'exists files of folder "/tmp"',
    "number-of-files-tmp": 'number of files of folder "/tmp"',
    "it-whose-singular": "(it) whose (it > 1) of 2",
    "it-whose-singular-empty": "(it) whose (it > 5) of 2",
    "time": "now - (now - 1 * day) > 1 * hour",
    "boolean-not": "not (1 = 2)",
    "string-contains": '"abc" contains "b"',
    "as-lowercase": "name of operating system as lowercase",
    "relay-missing": "exists relay service",
    "license-missing": 'exists setting "bfrcr_78_missing" of client',
    "setting-value-missing": 'value of setting "bfrcr_78_missing" of client',
    "settings-value-missing": 'values of settings "bfrcr_78_missing" of client',
    "first-line": 'line 1 of file "/etc/hosts"',
    "lines-number": 'number of lines of file "/etc/hosts"',
    "tuple-with-plural": "(1, (2;3))",
    "nested-tuple": "(1, (2, 3))",
    "tuple-string-comma-item": '("a, b", 1)',
    "literal-string-comma-no-space": '"a,b"',
    "literal-string-parens": '"(a)"',
    "literal-string-close-paren": '"a)"',
    "time-interval": "day",
    "version-literal": 'version "1.2"',
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def main(content_root: str) -> None:
    root = Path(content_root).expanduser()
    seen: set[str] = set()
    rows: list[dict[str, str]] = []

    def add(source: str, kind: str, text: str) -> None:
        key = _normalized(text)
        if not key or key in seen or len(key) > MAX_LENGTH:
            return
        seen.add(key)
        rows.append({"id": f"r{len(rows):04d}", "source": source, "kind": kind, "text": text})

    for name, text in EDGE_CASES.items():
        add(f"edge:{name}", "edge-case", text)

    rng = random.Random(SEED)
    by_directory: dict[Path, list[Path]] = {}
    for path in sorted(root.rglob("*.bes")):
        if ".claude" in path.parts or ".git" in path.parts:
            continue
        by_directory.setdefault(path.parent, []).append(path)
    for directory, paths in sorted(by_directory.items()):
        if len(paths) > PER_DIRECTORY_CAP:
            paths = sorted(rng.sample(paths, PER_DIRECTORY_CAP))
        for path in paths:
            try:
                sites = extract_relevance_from_file(path)
            except Exception as exc:  # noqa: BLE001 - a bad file is skipped, not fatal
                print(f"skipped {path}: {exc}", file=sys.stderr)  # noqa: T201
                continue
            for site in sites:
                if site.kind in KINDS and site.dialect in (Dialect.CLIENT, Dialect.UNCERTAIN):
                    add(str(path.relative_to(root)), site.kind, site.text)

    for row in rows:
        print(json.dumps(row))  # noqa: T201


if __name__ == "__main__":
    main(sys.argv[1])
